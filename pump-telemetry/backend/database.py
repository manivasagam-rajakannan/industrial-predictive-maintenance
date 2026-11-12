"""Store telemetry, keep the BI views current, and track pending manual guidance."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from model.pump_pipeline import FEATURES

BASE_VIEWS = Path(__file__).resolve().parents[1] / "model" / "live_views.sql"
SCHEMA = Path(__file__).with_name("schema.sql")
READING_WITH_GUIDANCE = """
    SELECT r.*, g.state AS guidance_state, g.result AS guidance
    FROM pump_readings AS r
    LEFT JOIN pump_guidance AS g USING (reading_id)
"""
INSERT_READING = """
    INSERT INTO pump_readings (
        event_id, source_time, pump_id, vibration_mm_s, temperature_c, pressure_bar,
        motor_current_amp, fault_score, alert_cutoff, anomaly_flag, suspected_fault,
        review_cue, source
    ) VALUES (
        %(event_id)s, %(source_time)s, %(pump_id)s, %(vibration_mm_s)s, %(temperature_c)s,
        %(pressure_bar)s, %(motor_current_amp)s, %(fault_score)s, %(alert_cutoff)s,
        %(anomaly_flag)s, %(suspected_fault)s, %(review_cue)s, %(source)s
    )
    ON CONFLICT (event_id) DO NOTHING
    RETURNING reading_id
"""


class EventConflict(ValueError):
    """An existing event ID belongs to a different sensor reading."""


class Database:
    def __init__(self):
        self.settings = {
            "host": os.environ.get("PUMP_DB_HOST", "127.0.0.1"),
            "port": int(os.environ.get("PUMP_DB_PORT", "55432")),
            "dbname": "pump_live",
            "user": "pump_owner",
            "password": os.environ.get("PUMP_DB_OWNER_PASSWORD", "local_pump_owner_demo"),
            "sslmode": os.environ.get("PUMP_DB_SSLMODE", "prefer"),
            "connect_timeout": 4,
        }
        self.reader_password = os.environ.get("PUMP_DB_READER_PASSWORD", "local_pump_reader_demo")

    def connect(self):
        return psycopg.connect(**self.settings, row_factory=dict_row)

    def initialize(self):
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(77162)")
            reader_exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = 'pump_reader'"
            ).fetchone()
            if reader_exists is None:
                conn.execute(sql.SQL("CREATE ROLE pump_reader LOGIN PASSWORD {}").format(
                    sql.Literal(self.reader_password)
                ))
            else:
                conn.execute(sql.SQL("ALTER ROLE pump_reader LOGIN PASSWORD {}").format(
                    sql.Literal(self.reader_password)
                ))
            conn.execute(BASE_VIEWS.read_text(encoding="utf-8"))
            conn.execute(SCHEMA.read_text(encoding="utf-8"))

    def save_readings(self, records: list[tuple[dict, bool]]):
        """Write the readings and their guidance jobs in one transaction."""
        with self.connect() as conn:
            return [self._save_reading(conn, reading, time_supplied) for reading, time_supplied in records]

    def _save_reading(self, conn, reading: dict, source_time_supplied: bool):
        row = conn.execute(INSERT_READING, reading).fetchone()
        if row is None:
            existing = conn.execute(
                READING_WITH_GUIDANCE + " WHERE r.event_id = %s", (reading["event_id"],)
            ).fetchone()
            check = ["pump_id", "source", *FEATURES]
            if source_time_supplied:
                check.append("source_time")
            if any(reading[key] != existing[key] for key in check):
                raise EventConflict("This event ID already belongs to a different reading.")
            return existing, True
        reading_id = row["reading_id"]
        if reading["anomaly_flag"]:
            conn.execute("INSERT INTO pump_guidance (reading_id) VALUES (%s)", (reading_id,))
        saved = conn.execute(READING_WITH_GUIDANCE + " WHERE r.reading_id = %s", (reading_id,)).fetchone()
        return saved, False

    def reading(self, reading_id: int):
        with self.connect() as conn:
            return conn.execute(
                READING_WITH_GUIDANCE + " WHERE r.reading_id = %s", (reading_id,)
            ).fetchone()

    def operator_snapshot(self):
        with self.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            status = conn.execute("SELECT * FROM powerbi_latest").fetchone()
            latest = conn.execute(
                READING_WITH_GUIDANCE + " ORDER BY r.reading_id DESC LIMIT 1"
            ).fetchone()
            timeline = conn.execute("""
                SELECT received_at, fault_score, alert_cutoff, anomaly_flag, source
                FROM pump_readings
                WHERE received_at >= statement_timestamp() - interval '2 minutes'
                ORDER BY reading_id DESC LIMIT 120
            """).fetchall()
            alerts = conn.execute(READING_WITH_GUIDANCE + """
                WHERE r.anomaly_flag = 1
                ORDER BY r.reading_id DESC LIMIT 12
            """).fetchall()
            return {"feed": status, "latest": latest, "timeline": timeline[::-1], "alerts": alerts}

    def claim_guidance(self):
        token = uuid4()
        with self.connect() as conn:
            return conn.execute("""
                WITH job AS (
                    SELECT reading_id FROM pump_guidance
                    WHERE state = 'queued' OR (state = 'running' AND lease_until < statement_timestamp())
                    ORDER BY reading_id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                ),
                claimed AS (
                    UPDATE pump_guidance AS g
                    SET state = 'running', claim_token = %s,
                        lease_until = statement_timestamp() + interval '90 seconds'
                    FROM job WHERE g.reading_id = job.reading_id
                    RETURNING g.reading_id, g.claim_token
                )
                SELECT r.*, c.claim_token
                FROM claimed AS c JOIN pump_readings AS r USING (reading_id)
            """, (token,)).fetchone()

    def finish_guidance(self, reading_id, token, result):
        state = "ready" if result is not None else "error"
        with self.connect() as conn:
            conn.execute("""
                UPDATE pump_guidance
                SET state = %s, result = %s, claim_token = NULL, lease_until = NULL,
                    completed_at = statement_timestamp()
                WHERE reading_id = %s AND claim_token = %s
            """, (state, Jsonb(result) if result is not None else None, reading_id, token))

    def ping(self):
        with self.connect() as conn:
            conn.execute("SELECT 1").fetchone()

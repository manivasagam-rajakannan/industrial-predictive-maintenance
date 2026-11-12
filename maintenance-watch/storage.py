"""Telemetry persistence for the local demo and the PostgreSQL DirectQuery path."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from scoring import FEATURES


COLUMNS = (
    "event_id", "asset_id", "source_mode", "observed_at", "ingested_at", "grade",
    *FEATURES, "model_score", "review_flag", "observed_failure", "source_udi", "model_version",
)
EVENT_CONTENT = ("asset_id", "source_mode", "observed_at", "grade", *FEATURES, "observed_failure", "source_udi")
SCHEMA = Path(__file__).with_name("storage_schema.sql")
BI_SCHEMA = Path(__file__).parent / "postgres" / "powerbi.sql"


def utc_now():
    return datetime.now(timezone.utc)


def utc_string(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class EventConflict(Exception):
    """An event ID already belongs to a different reading."""


class TelemetryStore:
    def __init__(self, sqlite_path, database_url=None, bi_password=None):
        self.path = Path(sqlite_path)
        self.database_url = database_url
        if self.database_url:
            with self.connect() as connection:
                if bi_password:
                    self.initialize_postgres(connection, bi_password)
                self.run(connection, "SELECT event_id FROM telemetry LIMIT 1").fetchone()
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(SCHEMA.read_text(encoding="utf-8"))

    def initialize_postgres(self, connection, bi_password):
        from psycopg import sql

        connection.execute("SELECT pg_advisory_xact_lock(77163)")
        reader = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = 'maintenance_bi'").fetchone()
        if reader:
            command = sql.SQL("ALTER ROLE maintenance_bi LOGIN PASSWORD {}")
        else:
            command = sql.SQL("CREATE ROLE maintenance_bi LOGIN PASSWORD {}")
        connection.execute(command.format(sql.Literal(bi_password)))
        connection.execute(SCHEMA.read_text(encoding="utf-8"))
        connection.execute(BI_SCHEMA.read_text(encoding="utf-8"))

    @contextmanager
    def connect(self):
        if self.database_url:
            import psycopg
            from psycopg.rows import dict_row

            connection = psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)
        else:
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def run(self, connection, sql, parameters=()):
        if self.database_url:
            sql = sql.replace("?", "%s")
        return connection.execute(sql, parameters)

    def save(self, records):
        placeholders = ", ".join("?" for _ in COLUMNS)
        insert = f"INSERT INTO telemetry ({', '.join(COLUMNS)}) VALUES ({placeholders}) ON CONFLICT(event_id) DO NOTHING"
        results = []
        with self.connect() as connection:
            for record in records:
                created = self.run(connection, insert, [record[column] for column in COLUMNS]).rowcount == 1
                if created:
                    results.append((record, True))
                    continue
                existing = dict(self.run(connection, "SELECT * FROM telemetry WHERE event_id = ?", (record["event_id"],)).fetchone())
                if any(existing[key] != record[key] for key in EVENT_CONTENT):
                    raise EventConflict("This event_id already belongs to a different reading.")
                results.append((existing, False))
        return results

    @staticmethod
    def filters(mode, minutes, asset=None, now=None):
        cutoff = utc_string((now or utc_now()) - timedelta(minutes=minutes))
        if asset:
            return "source_mode = ? AND observed_at >= ? AND asset_id = ?", (mode, cutoff, asset)
        return "source_mode = ? AND observed_at >= ?", (mode, cutoff)

    def snapshot(self, mode, minutes, asset):
        now = utc_now()
        fleet_where, fleet_args = self.filters(mode, minutes, now=now)
        where, args = self.filters(mode, minutes, asset, now)
        with self.connect() as connection:
            if self.database_url:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            else:
                connection.execute("BEGIN")
            summary = dict(self.run(connection, f"""
                SELECT COUNT(*) AS readings, COUNT(DISTINCT asset_id) AS assets,
                       COALESCE(SUM(review_flag), 0) AS reviews, MAX(ingested_at) AS last_ingested
                FROM telemetry WHERE {where}
            """, args).fetchone())
            fleet = self.run(connection, f"""
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY asset_id ORDER BY observed_at DESC, ingested_at DESC, event_id DESC
                    ) AS rank
                    FROM telemetry WHERE {fleet_where}
                ) AS ranked WHERE rank = 1 ORDER BY asset_id LIMIT 200
            """, fleet_args).fetchall()
            rows = self.run(connection, f"""
                SELECT * FROM telemetry WHERE {where} AND review_flag = 1
                ORDER BY observed_at DESC, event_id DESC LIMIT 12
            """, args).fetchall()
            minutes_data = self.run(connection, f"""
                SELECT substr(observed_at, 1, 16) AS minute_utc,
                       MAX(model_score) AS peak, AVG(model_score) AS mean,
                       COUNT(*) AS readings, SUM(review_flag) AS reviews
                FROM telemetry WHERE {where} GROUP BY 1 ORDER BY 1
            """, args).fetchall()
            latest = self.run(connection, f"""
                SELECT * FROM telemetry WHERE {where}
                ORDER BY observed_at DESC, ingested_at DESC, event_id DESC LIMIT 1
            """, args).fetchone()
        trend = [dict(row) for row in minutes_data]
        return {
            "summary": summary, "fleet": [dict(row) for row in fleet],
            "alerts": [dict(row) for row in rows], "trend": coalesce_trend(trend, minutes),
            "latest": dict(latest) if latest else None,
        }

    def recent_count(self, mode, minutes):
        where, args = self.filters(mode, minutes)
        with self.connect() as connection:
            return self.run(connection, f"SELECT COUNT(*) AS total FROM telemetry WHERE {where}", args).fetchone()["total"]

    def export(self, mode, minutes, asset):
        where, args = self.filters(mode, minutes, asset)
        with self.connect() as connection:
            rows = self.run(connection, f"""
                SELECT * FROM telemetry WHERE {where}
                ORDER BY observed_at DESC, event_id DESC LIMIT 10001
            """, args).fetchall()
        return [dict(row) for row in rows[:10000]], len(rows) > 10000


def coalesce_trend(rows, minutes):
    bucket_width = 15 if minutes == 1440 else 1
    buckets = {}
    for row in rows:
        minute = row["minute_utc"]
        bucket = minute[:14] + f"{int(minute[14:16]) // bucket_width * bucket_width:02d}"
        values = buckets.setdefault(bucket, {"at": bucket + ":00Z", "peak": 0, "score_sum": 0, "readings": 0, "reviews": 0})
        values["peak"] = max(values["peak"], row["peak"])
        values["score_sum"] += row["mean"] * row["readings"]
        values["readings"] += row["readings"]
        values["reviews"] += row["reviews"]
    for values in buckets.values():
        values["mean"] = values.pop("score_sum") / values["readings"]
    return list(buckets.values())

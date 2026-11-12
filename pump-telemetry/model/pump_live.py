"""Score simulated pump readings as they arrive and publish them for Power BI."""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fasteners
import psycopg

from live_db import LiveDatabase
from pump_pipeline import CALIBRATION_END, FEATURES, PROJECT_ROOT, fit_model, score_readings, simulate_telemetry

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
REPORT = PROJECT_ROOT / "Pump_Maintenance_Live.pbip"
INSERT_READING = """
    INSERT INTO pump_readings (
        source_time, pump_id, vibration_mm_s, temperature_c, pressure_bar,
        motor_current_amp, fault_score, alert_cutoff, anomaly_flag,
        suspected_fault, review_cue
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING reading_id
"""


def publish(database: LiveDatabase, passwords: dict[str, str], interval: float,
            limit: int, open_report: bool) -> None:
    telemetry = simulate_telemetry()
    model = fit_model(telemetry)
    replay = telemetry.iloc[CALIBRATION_END:].drop(columns="injected_fault").reset_index(drop=True)
    first_flags = model.classifier.predict_proba(replay[FEATURES])[:, 1] >= model.cutoff
    first_alert = next((index for index, flagged in enumerate(first_flags) if flagged), 0)
    offset = max(0, first_alert - 4)

    print(f"Live pump feed   {interval:g} second(s) per simulated reading")
    print(database.connection_details.read_text(encoding="utf-8"), end="")
    print(f"Saved connection details   {database.connection_details}")
    print(f"Power BI project   {REPORT.name}")
    print("Keep this window open. Press Ctrl+C to pause the feed.\n")

    count = 0
    next_tick = time.monotonic()
    next_cleanup = next_tick + 60
    with database.writer(passwords) as conn:
        while not limit or count < limit:
            index = (offset + count) % len(replay)
            sample = score_readings(replay.iloc[[index]], model).iloc[0]
            values = (
                sample["timestamp"].to_pydatetime(),
                sample["machine_id"],
                *(float(sample[channel]) for channel in FEATURES),
                float(sample["fault_score"]),
                float(sample["alert_cutoff"]),
                int(sample["anomaly_flag"]),
                sample["suspected_fault"],
                sample["review_cue"],
            )
            reading_id = conn.execute(INSERT_READING, values).fetchone()[0]
            status = sample["suspected_fault"] or "Monitoring"
            now = datetime.now(IST).strftime("%H:%M:%S")
            line = f"{now} IST   Reading {reading_id:>5}   Score {sample['fault_score']:>5.0%}   {status}"
            print(f"\r{line:<79}", end="", flush=True)
            if count == 0 and open_report and os.name == "nt":
                try:
                    os.startfile(REPORT)
                except OSError:
                    print(f"\nOpen {REPORT.name} from Power BI Desktop.\n")
            count += 1
            if time.monotonic() >= next_cleanup:
                conn.execute("DELETE FROM pump_readings WHERE received_at < statement_timestamp() - interval '2 hours'")
                next_cleanup = time.monotonic() + 60
            next_tick = max(next_tick + interval, time.monotonic())
            if not limit or count < limit:
                time.sleep(max(0, next_tick - time.monotonic()))
    print(f"\nPublished {count} readings.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=PROJECT_ROOT / ".runtime")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-readings", type=int, default=0)
    parser.add_argument("--open-report", action="store_true")
    parser.add_argument("--stop-db", action="store_true")
    args = parser.parse_args()
    if not 0.05 <= args.interval <= 5 or args.max_readings < 0:
        parser.error("Choose a reading interval from 0.05 to 5 seconds and a nonnegative reading count.")
    db = LiveDatabase(args.runtime)
    lease = fasteners.InterProcessLock(str(db.runtime / "publisher.lock"))

    try:
        if args.stop_db:
            db.stop()
            return 0
        db.runtime.mkdir(parents=True, exist_ok=True)
        if not lease.acquire(blocking=False):
            print("The feed is already running. Use the open Live Monitor report.")
            return 0
        try:
            print("Starting the local pump database and training the model...")
            passwords = db.start()
            publish(db, passwords, args.interval, args.max_readings, args.open_report)
        finally:
            lease.release()
    except KeyboardInterrupt:
        print("\nFeed paused. Run this script again to resume the replay. Use --stop-db to close its database.")
    except (OSError, RuntimeError, psycopg.Error) as error:
        print(f"\nThe live feed could not start or continue. {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

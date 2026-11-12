"""Validate uploaded pump readings before sending them to the scoring API."""

import hashlib
import math
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from telemetry_files import FORMATS, FORMAT_HELP, normalize_heading, read_table

SENSORS = ("vibration_mm_s", "temperature_c", "pressure_bar", "motor_current_amp")
OPTIONAL = ("timestamp", "event_id", "pump_id")
MAX_ROWS = 200
MAX_BYTES = 1_000_000
ALIASES = {
    "vibration_mm_sec": "vibration_mm_s",
    "temperature_deg_c": "temperature_c",
    "motor_current_a": "motor_current_amp",
    "motor_current_amps": "motor_current_amp",
}
SAMPLE_CSV = (
    "vibration_mm_s,temperature_c,pressure_bar,motor_current_amp\n"
    "1.855,65.83,4.43,14.32\n"
    "1.588,88.41,4.35,17.87\n"
)


def parse_readings(content: bytes, filename: str) -> list[dict]:
    (_, headings), *rows = read_table(content, filename, max_rows=MAX_ROWS, max_bytes=MAX_BYTES)
    columns = {}
    for index, heading in enumerate(headings):
        key = normalize_heading(heading)
        key = ALIASES.get(key, key)
        if key not in (*SENSORS, *OPTIONAL):
            continue
        if key in columns:
            raise ValueError(f"Use only one column for {key}.")
        columns[key] = index
    missing = set(SENSORS) - columns.keys()
    if missing:
        raise ValueError("Add these pump columns. " + ", ".join(sorted(missing)))

    digest = hashlib.sha256(content).hexdigest()
    readings = []
    for number, row in rows:
        if len(row) != len(headings):
            raise ValueError(f"Row {number} has a different number of columns from the header.")
        values = {}
        for sensor in SENSORS:
            try:
                value = float(row[columns[sensor]])
                if not math.isfinite(value):
                    raise ValueError
            except ValueError as error:
                raise ValueError(f"Row {number} needs a finite number for {sensor}.") from error
            values[sensor] = value
        values.update({key: row[columns[key]] for key in OPTIONAL if key in columns and row[columns[key]]})
        # Keep IDs stable for CSVs already uploaded by the earlier release.
        values.setdefault("event_id", str(uuid5(NAMESPACE_URL, f"pump-csv:{digest}:{number}")))
        readings.append(values)
    return readings

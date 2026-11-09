CREATE TABLE IF NOT EXISTS telemetry (
    event_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    source_mode TEXT NOT NULL CHECK (source_mode IN ('replay', 'live')),
    observed_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    grade TEXT NOT NULL CHECK (grade IN ('H', 'L', 'M')),
    air_temperature_k DOUBLE PRECISION NOT NULL,
    process_temperature_k DOUBLE PRECISION NOT NULL,
    rotational_speed_rpm DOUBLE PRECISION NOT NULL,
    torque_nm DOUBLE PRECISION NOT NULL,
    tool_wear_min DOUBLE PRECISION NOT NULL,
    model_score DOUBLE PRECISION NOT NULL CHECK (model_score BETWEEN 0 AND 1),
    review_flag INTEGER NOT NULL CHECK (review_flag IN (0, 1)),
    observed_failure INTEGER CHECK (observed_failure IN (0, 1)),
    source_udi INTEGER,
    model_version TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS telemetry_source_time ON telemetry (source_mode, observed_at);
CREATE INDEX IF NOT EXISTS telemetry_asset_time ON telemetry (source_mode, asset_id, observed_at);

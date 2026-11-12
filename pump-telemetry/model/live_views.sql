CREATE TABLE IF NOT EXISTS pump_readings (
    reading_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    source_time timestamp NOT NULL,
    pump_id text NOT NULL,
    vibration_mm_s double precision NOT NULL,
    temperature_c double precision NOT NULL,
    pressure_bar double precision NOT NULL,
    motor_current_amp double precision NOT NULL,
    fault_score double precision NOT NULL CHECK (fault_score BETWEEN 0 AND 1),
    alert_cutoff double precision NOT NULL CHECK (alert_cutoff BETWEEN 0 AND 1),
    anomaly_flag smallint NOT NULL CHECK (anomaly_flag IN (0, 1)),
    suspected_fault text NOT NULL,
    review_cue text NOT NULL
);

CREATE INDEX IF NOT EXISTS pump_readings_received ON pump_readings (received_at DESC);
CREATE INDEX IF NOT EXISTS pump_alerts_received ON pump_readings (received_at DESC)
    WHERE anomaly_flag = 1;

CREATE OR REPLACE VIEW powerbi_latest AS
SELECT
    CASE
        WHEN latest.reading_id IS NULL THEN 'Waiting for feed'
        WHEN latest.received_at < statement_timestamp() - interval '10 seconds' THEN 'Feed paused'
        WHEN EXISTS (
            SELECT 1 FROM pump_readings
            WHERE anomaly_flag = 1 AND received_at >= statement_timestamp() - interval '15 seconds'
        ) THEN 'Recent alert'
        ELSE 'Monitoring'
    END AS feed_state,
    to_char(latest.received_at AT TIME ZONE INTERVAL '05:30', 'HH24:MI:SS') AS reading_clock_ist,
    latest.reading_id,
    latest.vibration_mm_s,
    latest.temperature_c,
    latest.pressure_bar,
    latest.motor_current_amp,
    latest.fault_score
FROM (VALUES (1)) AS anchor(one_row)
LEFT JOIN LATERAL (
    SELECT * FROM pump_readings ORDER BY reading_id DESC LIMIT 1
) AS latest ON true;

CREATE OR REPLACE VIEW powerbi_readings AS
SELECT
    reading_id,
    received_at AT TIME ZONE INTERVAL '05:30' AS reading_time_ist,
    fault_score,
    alert_cutoff,
    anomaly_flag
FROM pump_readings
WHERE received_at >= statement_timestamp() - interval '5 minutes';

CREATE OR REPLACE VIEW powerbi_alerts AS
SELECT
    reading_id,
    received_at AT TIME ZONE INTERVAL '05:30' AS alert_time_ist,
    suspected_fault,
    fault_score,
    review_cue
FROM pump_readings
WHERE anomaly_flag = 1
    AND received_at >= statement_timestamp() - interval '5 minutes'
ORDER BY received_at DESC, reading_id DESC
LIMIT 30;

GRANT CONNECT ON DATABASE pump_live TO pump_reader;
GRANT USAGE ON SCHEMA public TO pump_reader;
GRANT SELECT ON powerbi_latest, powerbi_readings, powerbi_alerts TO pump_reader;

ALTER TABLE pump_readings
    ADD COLUMN IF NOT EXISTS event_id uuid,
    ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'api';
CREATE UNIQUE INDEX IF NOT EXISTS pump_readings_event_id ON pump_readings (event_id);

CREATE TABLE IF NOT EXISTS pump_guidance (
    reading_id bigint PRIMARY KEY REFERENCES pump_readings(reading_id) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'ready', 'error')),
    claim_token uuid,
    lease_until timestamptz,
    result jsonb,
    completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS pump_guidance_queue ON pump_guidance(reading_id)
    WHERE state IN ('queued', 'running');

CREATE OR REPLACE VIEW tableau_telemetry AS
SELECT reading_id, received_at AT TIME ZONE INTERVAL '05:30' AS reading_time_ist,
       pump_id, vibration_mm_s, temperature_c, pressure_bar, motor_current_amp,
       fault_score, alert_cutoff, anomaly_flag::integer AS anomaly_flag,
       CASE WHEN anomaly_flag = 1 THEN suspected_fault ELSE 'Monitoring' END AS fault_group,
       review_cue
FROM pump_readings
WHERE received_at >= statement_timestamp() - interval '1 hour';

GRANT SELECT ON tableau_telemetry TO pump_reader;

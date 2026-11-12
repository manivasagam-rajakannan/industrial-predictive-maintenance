CREATE OR REPLACE VIEW public.powerbi_telemetry AS
SELECT
    event_id,
    asset_id,
    CASE WHEN source_mode = 'replay' THEN 'Synthetic replay' ELSE 'Live telemetry' END AS data_source,
    observed_at::timestamptz AT TIME ZONE 'UTC' AS reading_utc,
    ingested_at::timestamptz AT TIME ZONE 'UTC' AS ingestion_utc,
    date_trunc('minute', observed_at::timestamptz AT TIME ZONE 'UTC') AS minute_utc,
    grade,
    air_temperature_k,
    process_temperature_k,
    rotational_speed_rpm,
    torque_nm,
    tool_wear_min,
    model_score,
    review_flag,
    CASE WHEN review_flag = 1 THEN 'Review' ELSE 'Normal score' END AS score_status,
    observed_failure,
    model_version
FROM public.telemetry
WHERE observed_at >= to_char(
    (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '24 hours',
    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
);

GRANT USAGE ON SCHEMA public TO maintenance_bi;
GRANT SELECT ON public.powerbi_telemetry TO maintenance_bi;

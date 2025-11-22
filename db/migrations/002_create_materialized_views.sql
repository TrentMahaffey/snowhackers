-- Create materialized view for forecast rollup (used by dashboard)
-- This pre-computes the expensive window function queries

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_forecast_rollup AS
WITH latest AS (
  SELECT f.*,
         ROW_NUMBER() OVER (
           PARTITION BY
             COALESCE(f.station_triplet, CONCAT(f.latitude, ',', f.longitude)),
             f.model_name,
             f.ts_valid
           ORDER BY f.ts_forecast DESC
         ) AS rn
  FROM forecast_hourly f
  WHERE f.ts_valid > now()
    AND f.ts_valid <= now() + interval '72 hours'
),
per_model AS (
  SELECT
    COALESCE(f.station_triplet, CONCAT(f.latitude, ',', f.longitude)) AS st_key,
    f.station_triplet,
    f.model_name,
    f.latitude,
    f.longitude,
    -- sum within window per model
    SUM(CASE WHEN f.ts_valid <= now() + interval '24 hours' THEN COALESCE(f.snowfall_cm,0) ELSE 0 END)::numeric AS h24_cm,
    SUM(CASE WHEN f.ts_valid <= now() + interval '48 hours' THEN COALESCE(f.snowfall_cm,0) ELSE 0 END)::numeric AS h48_cm,
    SUM(CASE WHEN f.ts_valid <= now() + interval '72 hours' THEN COALESCE(f.snowfall_cm,0) ELSE 0 END)::numeric AS h72_cm
  FROM latest f
  WHERE f.rn = 1
  GROUP BY COALESCE(f.station_triplet, CONCAT(f.latitude, ',', f.longitude)),
           f.station_triplet,
           f.model_name,
           f.latitude,
           f.longitude
),
by_station AS (
  SELECT
    p.st_key,
    MAX(p.station_triplet) FILTER (WHERE p.station_triplet IS NOT NULL) AS station_triplet,
    MAX(p.latitude) AS latitude,
    MAX(p.longitude) AS longitude,
    MAX(p.h24_cm) AS h24_cm_max,
    AVG(p.h24_cm) AS h24_cm_avg,
    MAX(p.h48_cm) AS h48_cm_max,
    AVG(p.h48_cm) AS h48_cm_avg,
    MAX(p.h72_cm) AS h72_cm_max,
    AVG(p.h72_cm) AS h72_cm_avg
  FROM per_model p
  GROUP BY p.st_key
)
SELECT
  COALESCE(s.name, b.st_key) AS station_name,
  COALESCE(b.station_triplet, b.st_key) AS station_triplet,
  b.latitude,
  b.longitude,
  ROUND((COALESCE(b.h24_cm_max,0) / 2.54)::numeric, 1) AS h24_max_in,
  ROUND((COALESCE(b.h24_cm_avg,0) / 2.54)::numeric, 1) AS h24_avg_in,
  ROUND((COALESCE(b.h48_cm_max,0) / 2.54)::numeric, 1) AS h48_max_in,
  ROUND((COALESCE(b.h48_cm_avg,0) / 2.54)::numeric, 1) AS h48_avg_in,
  ROUND((COALESCE(b.h72_cm_max,0) / 2.54)::numeric, 1) AS h72_max_in,
  ROUND((COALESCE(b.h72_cm_avg,0) / 2.54)::numeric, 1) AS h72_avg_in
FROM by_station b
LEFT JOIN snotel_station s ON s.station_triplet = b.station_triplet;

-- Create index on materialized view
CREATE INDEX IF NOT EXISTS idx_mv_forecast_rollup_station ON mv_forecast_rollup(station_triplet);
CREATE INDEX IF NOT EXISTS idx_mv_forecast_rollup_h24_max ON mv_forecast_rollup(h24_max_in DESC);
CREATE INDEX IF NOT EXISTS idx_mv_forecast_rollup_h48_max ON mv_forecast_rollup(h48_max_in DESC);
CREATE INDEX IF NOT EXISTS idx_mv_forecast_rollup_h72_max ON mv_forecast_rollup(h72_max_in DESC);

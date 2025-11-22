-- 48 hour view
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_obs_vs_fc_48h AS
WITH params AS (
  SELECT
    now() - make_interval(hours => 48) AS start_ts,
    now() AS end_ts,
    (CURRENT_DATE - make_interval(days => 2)) AS start_date
),
obs AS (
  SELECT a.station_triplet,
         SUM(COALESCE(a.new_snow_cm,0))::numeric AS obs_cm
  FROM snotel_daily_accums_cm a, params p
  WHERE a.date >= p.start_date
  GROUP BY a.station_triplet
),
latest AS (
  SELECT f.*,
         CASE
           WHEN LOWER(f.model_name) LIKE 'ecmwf%' THEN 'ecmwf'
           WHEN LOWER(f.model_name) LIKE 'gfs%'   THEN 'gfs'
           WHEN LOWER(f.model_name) LIKE 'icon%'  THEN 'icon'
           ELSE LOWER(f.model_name)
         END AS model_bucket,
         ROW_NUMBER() OVER (
           PARTITION BY f.station_triplet,
                        CASE
                          WHEN LOWER(f.model_name) LIKE 'ecmwf%' THEN 'ecmwf'
                          WHEN LOWER(f.model_name) LIKE 'gfs%'   THEN 'gfs'
                          WHEN LOWER(f.model_name) LIKE 'icon%'  THEN 'icon'
                          ELSE LOWER(f.model_name)
                        END,
                        f.ts_valid
           ORDER BY f.ts_forecast DESC
         ) AS rn
  FROM forecast_hourly f, params p
  WHERE f.ts_valid > p.start_ts
    AND f.ts_valid <= p.end_ts
    AND f.ts_forecast <= f.ts_valid
),
fc AS (
  SELECT
    station_triplet,
    model_bucket,
    SUM(CASE WHEN rn=1 THEN COALESCE(snowfall_cm,0) ELSE 0 END)::numeric AS fc_cm
  FROM latest
  GROUP BY station_triplet, model_bucket
),
fc_pivot AS (
  SELECT station_triplet,
         SUM(CASE WHEN model_bucket='ecmwf' THEN fc_cm ELSE 0 END) AS ecmwf_cm,
         SUM(CASE WHEN model_bucket='gfs'   THEN fc_cm ELSE 0 END) AS gfs_cm,
         SUM(CASE WHEN model_bucket='icon'  THEN fc_cm ELSE 0 END) AS icon_cm
  FROM fc
  GROUP BY station_triplet
)
SELECT s.name AS station_name, s.station_triplet,
       ROUND(COALESCE(o.obs_cm,0), 1) AS obs_cm,
       ROUND((COALESCE(o.obs_cm,0) / 2.54)::numeric, 1) AS obs_in,
       ROUND(COALESCE(p.ecmwf_cm,0), 1) AS ecmwf_cm,
       ROUND((COALESCE(p.ecmwf_cm,0) / 2.54)::numeric, 1) AS ecmwf_in,
       ROUND(COALESCE(p.gfs_cm,0), 1) AS gfs_cm,
       ROUND((COALESCE(p.gfs_cm,0) / 2.54)::numeric, 1) AS gfs_in,
       ROUND(COALESCE(p.icon_cm,0), 1) AS icon_cm,
       ROUND((COALESCE(p.icon_cm,0) / 2.54)::numeric, 1) AS icon_in
FROM snotel_station s
LEFT JOIN obs o ON o.station_triplet = s.station_triplet
LEFT JOIN fc_pivot p ON p.station_triplet = s.station_triplet
ORDER BY COALESCE(o.obs_cm,0) DESC
LIMIT 10;

-- 72 hour view  
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_obs_vs_fc_72h AS
WITH params AS (
  SELECT
    now() - make_interval(hours => 72) AS start_ts,
    now() AS end_ts,
    (CURRENT_DATE - make_interval(days => 3)) AS start_date
),
obs AS (
  SELECT a.station_triplet,
         SUM(COALESCE(a.new_snow_cm,0))::numeric AS obs_cm
  FROM snotel_daily_accums_cm a, params p
  WHERE a.date >= p.start_date
  GROUP BY a.station_triplet
),
latest AS (
  SELECT f.*,
         CASE
           WHEN LOWER(f.model_name) LIKE 'ecmwf%' THEN 'ecmwf'
           WHEN LOWER(f.model_name) LIKE 'gfs%'   THEN 'gfs'
           WHEN LOWER(f.model_name) LIKE 'icon%'  THEN 'icon'
           ELSE LOWER(f.model_name)
         END AS model_bucket,
         ROW_NUMBER() OVER (
           PARTITION BY f.station_triplet,
                        CASE
                          WHEN LOWER(f.model_name) LIKE 'ecmwf%' THEN 'ecmwf'
                          WHEN LOWER(f.model_name) LIKE 'gfs%'   THEN 'gfs'
                          WHEN LOWER(f.model_name) LIKE 'icon%'  THEN 'icon'
                          ELSE LOWER(f.model_name)
                        END,
                        f.ts_valid
           ORDER BY f.ts_forecast DESC
         ) AS rn
  FROM forecast_hourly f, params p
  WHERE f.ts_valid > p.start_ts
    AND f.ts_valid <= p.end_ts
    AND f.ts_forecast <= f.ts_valid
),
fc AS (
  SELECT
    station_triplet,
    model_bucket,
    SUM(CASE WHEN rn=1 THEN COALESCE(snowfall_cm,0) ELSE 0 END)::numeric AS fc_cm
  FROM latest
  GROUP BY station_triplet, model_bucket
),
fc_pivot AS (
  SELECT station_triplet,
         SUM(CASE WHEN model_bucket='ecmwf' THEN fc_cm ELSE 0 END) AS ecmwf_cm,
         SUM(CASE WHEN model_bucket='gfs'   THEN fc_cm ELSE 0 END) AS gfs_cm,
         SUM(CASE WHEN model_bucket='icon'  THEN fc_cm ELSE 0 END) AS icon_cm
  FROM fc
  GROUP BY station_triplet
)
SELECT s.name AS station_name, s.station_triplet,
       ROUND(COALESCE(o.obs_cm,0), 1) AS obs_cm,
       ROUND((COALESCE(o.obs_cm,0) / 2.54)::numeric, 1) AS obs_in,
       ROUND(COALESCE(p.ecmwf_cm,0), 1) AS ecmwf_cm,
       ROUND((COALESCE(p.ecmwf_cm,0) / 2.54)::numeric, 1) AS ecmwf_in,
       ROUND(COALESCE(p.gfs_cm,0), 1) AS gfs_cm,
       ROUND((COALESCE(p.gfs_cm,0) / 2.54)::numeric, 1) AS gfs_in,
       ROUND(COALESCE(p.icon_cm,0), 1) AS icon_cm,
       ROUND((COALESCE(p.icon_cm,0) / 2.54)::numeric, 1) AS icon_in
FROM snotel_station s
LEFT JOIN obs o ON o.station_triplet = s.station_triplet
LEFT JOIN fc_pivot p ON p.station_triplet = s.station_triplet
ORDER BY COALESCE(o.obs_cm,0) DESC
LIMIT 10;

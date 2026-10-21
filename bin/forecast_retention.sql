-- Nightly forecast_hourly retention (2026-10-20): runs older than 48 h keep only lead -24 h..+72 h.
-- That is all the map rollup, obs-vs-forecast views and day-ahead skill scoring use.
-- Each nightly pull adds ~1.3M rows; this removes the ~1.1M long-lead rows from runs that just aged out.
-- Autovacuum makes the space reusable, so the table stays roughly flat.
DELETE FROM forecast_hourly
WHERE ts_forecast < now() - interval '48 hours'
  AND (ts_valid > ts_forecast + interval '72 hours' OR ts_valid < ts_forecast - interval '24 hours');

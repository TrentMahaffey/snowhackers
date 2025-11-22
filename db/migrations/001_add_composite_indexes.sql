-- Add composite indexes to optimize common query patterns
-- These indexes will dramatically speed up queries that filter by station and time

-- Composite index for station + time queries (most common pattern)
CREATE INDEX CONCURRENTLY IF NOT EXISTS forecast_hourly_station_valid_idx
ON forecast_hourly (station_triplet, ts_valid DESC);

-- Composite index for model + time queries
CREATE INDEX CONCURRENTLY IF NOT EXISTS forecast_hourly_model_valid_idx
ON forecast_hourly (model_name, ts_valid DESC);

-- Composite index for station + model + time (covers most CTE queries)
CREATE INDEX CONCURRENTLY IF NOT EXISTS forecast_hourly_station_model_valid_idx
ON forecast_hourly (station_triplet, model_name, ts_valid DESC);

-- Index for snotel daily accums by station and date
CREATE INDEX CONCURRENTLY IF NOT EXISTS snotel_daily_raw_station_date_idx
ON snotel_daily_raw (station_triplet, date DESC);

-- Analyze tables to update statistics
ANALYZE forecast_hourly;
ANALYZE snotel_daily_raw;
ANALYZE snotel_hourly_obs;

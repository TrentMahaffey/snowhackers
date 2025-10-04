-- Keep everything in UTC; convert at the UI.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- SNOTEL stations (one row per station)
CREATE TABLE IF NOT EXISTS snotel_station (
  station_triplet TEXT PRIMARY KEY,     -- e.g., '663:CO:SNTL'
  name           TEXT NOT NULL,
  latitude       DOUBLE PRECISION NOT NULL,
  longitude      DOUBLE PRECISION NOT NULL,
  elevation_m    DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS snotel_station_latlon_idx
  ON snotel_station (latitude, longitude);

-- Hourly forecast per model per issuance time & valid time
CREATE TABLE IF NOT EXISTS forecast_hourly (
  id            BIGSERIAL PRIMARY KEY,
  model_name    TEXT NOT NULL,          -- 'gfs' | 'icon' | 'ecmwf' | 'blend'
  ts_forecast   TIMESTAMPTZ NOT NULL,   -- model run / issuance time
  ts_valid      TIMESTAMPTZ NOT NULL,   -- forecast valid hour
  latitude      DOUBLE PRECISION NOT NULL,
  longitude     DOUBLE PRECISION NOT NULL,
  snowfall_cm   NUMERIC,                -- hourly new snowfall
  source        TEXT DEFAULT 'open-meteo', -- provenance
  UNIQUE (model_name, ts_forecast, ts_valid, latitude, longitude)
);

CREATE INDEX IF NOT EXISTS forecast_hourly_valid_idx
  ON forecast_hourly (ts_valid DESC);

-- Optional: link SNOTEL to nearest forecast point later with a mapping table.
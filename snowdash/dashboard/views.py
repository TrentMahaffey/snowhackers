from django.db import connection
from django.shortcuts import render

def _fetchall_dict(cur):
    if cur.description is None:
        return []
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

# Replace your _OBS_VS_FC_SQL with this version that
#  - uses daily SNOTEL accums for obs (last N days)
#  - uses forecast_hourly (not v_forecast_vs_actual) with "latest run <= valid"
# Params: (hours_back, days_back)
_OBS_VS_FC_SQL = """
WITH params AS (
  SELECT
    now() - make_interval(hours => %s) AS start_ts,
    now()                              AS end_ts,
    (CURRENT_DATE - make_interval(days => %s)) AS start_date
),
-- Observed from DAILY accums (works even if hourly table is empty)
obs AS (
  SELECT a.station_triplet,
         SUM(COALESCE(a.new_snow_cm,0))::numeric AS obs_cm
  FROM public.snotel_daily_accums_cm a, params p
  WHERE a.date >= p.start_date
  GROUP BY a.station_triplet
),

-- Forecasts: latest run <= valid per (station_triplet, model_variant, ts_valid)
latest AS (
  SELECT f.*,
         -- Normalize model names so icon/icon-d2/icon_seamless all map to 'icon'
         CASE
           WHEN LOWER(f.model_name) LIKE 'ecmwf%%' THEN 'ecmwf'
           WHEN LOWER(f.model_name) LIKE 'gfs%%'   THEN 'gfs'
           WHEN LOWER(f.model_name) LIKE 'icon%%'  THEN 'icon'
           ELSE LOWER(f.model_name)
         END AS model_bucket,
         ROW_NUMBER() OVER (
           PARTITION BY f.station_triplet,
                        -- partition by the bucket, not the raw name
                        CASE
                          WHEN LOWER(f.model_name) LIKE 'ecmwf%%' THEN 'ecmwf'
                          WHEN LOWER(f.model_name) LIKE 'gfs%%'   THEN 'gfs'
                          WHEN LOWER(f.model_name) LIKE 'icon%%'  THEN 'icon'
                          ELSE LOWER(f.model_name)
                        END,
                        f.ts_valid
           ORDER BY f.ts_forecast DESC
         ) AS rn
  FROM public.forecast_hourly f, params p
  WHERE f.ts_valid > p.start_ts
    AND f.ts_valid <= p.end_ts
    AND f.ts_forecast <= f.ts_valid
),

-- Sum by normalized bucket
fc AS (
  SELECT
    station_triplet,
    model_bucket,
    SUM(CASE WHEN rn=1 THEN COALESCE(snowfall_cm,0) ELSE 0 END)::numeric AS fc_cm
  FROM latest
  GROUP BY station_triplet, model_bucket
),

-- Pivot to columns
fc_pivot AS (
  SELECT station_triplet,
         SUM(CASE WHEN model_bucket='ecmwf' THEN fc_cm ELSE 0 END) AS ecmwf_cm,
         SUM(CASE WHEN model_bucket='gfs'   THEN fc_cm ELSE 0 END) AS gfs_cm,
         SUM(CASE WHEN model_bucket='icon'  THEN fc_cm ELSE 0 END) AS icon_cm
  FROM fc
  GROUP BY station_triplet
)

SELECT s.name AS station_name, s.station_triplet,
       ROUND(COALESCE(o.obs_cm,0), 1)                               AS obs_cm,
       ROUND((COALESCE(o.obs_cm,0) / 2.54)::numeric, 1)             AS obs_in,
       ROUND(COALESCE(p.ecmwf_cm,0), 1)                              AS ecmwf_cm,
       ROUND((COALESCE(p.ecmwf_cm,0) / 2.54)::numeric, 1)           AS ecmwf_in,
       ROUND(COALESCE(p.gfs_cm,0), 1)                                AS gfs_cm,
       ROUND((COALESCE(p.gfs_cm,0) / 2.54)::numeric, 1)             AS gfs_in,
       ROUND(COALESCE(p.icon_cm,0), 1)                               AS icon_cm,
       ROUND((COALESCE(p.icon_cm,0) / 2.54)::numeric, 1)            AS icon_in
FROM public.snotel_station s
LEFT JOIN obs      o ON o.station_triplet = s.station_triplet
LEFT JOIN fc_pivot p ON p.station_triplet = s.station_triplet
ORDER BY COALESCE(o.obs_cm,0) DESC,
         COALESCE(p.ecmwf_cm,0)+COALESCE(p.gfs_cm,0)+COALESCE(p.icon_cm,0) DESC
LIMIT 10;
"""



import math

def _observed_with_model_forecasts(hours_back: int):
    days_back = int(math.ceil(hours_back / 24))
    with connection.cursor() as cur:
        cur.execute(_OBS_VS_FC_SQL, [hours_back, days_back])
        return _fetchall_dict(cur)



def index(request):
    with connection.cursor() as cur:
        # --- counts ---
        cur.execute("SELECT COUNT(*) AS stations FROM public.snotel_station;")
        stations = _fetchall_dict(cur)[0]["stations"]

        cur.execute("SELECT COUNT(*) AS hourly_obs FROM public.snotel_hourly_obs;")
        hourly_obs = _fetchall_dict(cur)[0]["hourly_obs"]

        cur.execute("SELECT COUNT(*) AS forecast_rows FROM public.forecast_hourly;")
        forecast_rows = _fetchall_dict(cur)[0]["forecast_rows"]

        counts = {"stations": stations, "hourly_obs": hourly_obs, "forecast_rows": forecast_rows}

        # --- combined forecast rollup: per-station MAX & AVG across models for 24/48/72 ---
        cur.execute("""
        WITH latest AS (
          SELECT f.*,
                 ROW_NUMBER() OVER (
                   PARTITION BY
                     COALESCE(f.station_triplet, CONCAT(f.latitude, ',', f.longitude)),
                     f.model_name,
                     f.ts_valid
                   ORDER BY f.ts_forecast DESC
                 ) AS rn
          FROM public.forecast_hourly f
          WHERE f.ts_valid > now()
            AND f.ts_valid <= now() + interval '72 hours'
        ),
        per_model AS (
          SELECT
            COALESCE(f.station_triplet, CONCAT(f.latitude, ',', f.longitude)) AS st_key,
            f.station_triplet,
            f.model_name,
            -- sum within window per model
            SUM(CASE WHEN f.ts_valid <= now() + interval '24 hours' THEN COALESCE(f.snowfall_cm,0) ELSE 0 END)::numeric AS h24_cm,
            SUM(CASE WHEN f.ts_valid <= now() + interval '48 hours' THEN COALESCE(f.snowfall_cm,0) ELSE 0 END)::numeric AS h48_cm,
            SUM(CASE WHEN f.ts_valid <= now() + interval '72 hours' THEN COALESCE(f.snowfall_cm,0) ELSE 0 END)::numeric AS h72_cm
          FROM latest f
          WHERE f.rn = 1
          GROUP BY COALESCE(f.station_triplet, CONCAT(f.latitude, ',', f.longitude)),
                   f.station_triplet,
                   f.model_name
        ),
        by_station AS (
          SELECT
            p.st_key,
            MAX(p.station_triplet) FILTER (WHERE p.station_triplet IS NOT NULL) AS station_triplet,
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
          -- convert to inches in SQL; ensure numeric then round
          ROUND((COALESCE(b.h24_cm_max,0) / 2.54)::numeric, 1) AS h24_max_in,
          ROUND((COALESCE(b.h24_cm_avg,0) / 2.54)::numeric, 1) AS h24_avg_in,
          ROUND((COALESCE(b.h48_cm_max,0) / 2.54)::numeric, 1) AS h48_max_in,
          ROUND((COALESCE(b.h48_cm_avg,0) / 2.54)::numeric, 1) AS h48_avg_in,
          ROUND((COALESCE(b.h72_cm_max,0) / 2.54)::numeric, 1) AS h72_max_in,
          ROUND((COALESCE(b.h72_cm_avg,0) / 2.54)::numeric, 1) AS h72_avg_in
        FROM by_station b
        LEFT JOIN public.snotel_station s ON s.station_triplet = b.station_triplet
        """)
        rows = _fetchall_dict(cur)

    # leaderboards: sort by MAX for each interval and take top 10
        def topn(rows, key, n=10):
            return sorted(rows, key=lambda r: (r.get(key) or 0), reverse=True)[:n]

        top_forecasts = {
            "h24": [
                {"station_name": r["station_name"], "station_triplet": r["station_triplet"],
                 "h24_max_in": r["h24_max_in"], "h24_avg_in": r["h24_avg_in"]}
                for r in topn(rows, "h24_max_in")
            ],
            "h48": [
                {"station_name": r["station_name"], "station_triplet": r["station_triplet"],
                 "h48_max_in": r["h48_max_in"], "h48_avg_in": r["h48_avg_in"]}
                for r in topn(rows, "h48_max_in")
            ],
            "h72": [
                {"station_name": r["station_name"], "station_triplet": r["station_triplet"],
                 "h72_max_in": r["h72_max_in"], "h72_avg_in": r["h72_avg_in"]}
                for r in topn(rows, "h72_max_in")
            ],
        }

        # --- observed totals (SNOTEL) — top 10 for 24/48/72 using daily accums view ---
        cur.execute("""
                WITH recent AS (
                  SELECT a.station_triplet, a.date, a.new_snow_cm
                  FROM public.snotel_daily_accums_cm a
                  WHERE a.date >= CURRENT_DATE - INTERVAL '3 days'
                ),
                agg AS (
                  SELECT
                    r.station_triplet,
                    SUM(CASE WHEN r.date >= CURRENT_DATE - INTERVAL '1 day' THEN COALESCE(r.new_snow_cm,0) ELSE 0 END)::numeric AS h24_cm,
                    SUM(CASE WHEN r.date >= CURRENT_DATE - INTERVAL '2 days' THEN COALESCE(r.new_snow_cm,0) ELSE 0 END)::numeric AS h48_cm,
                    SUM(COALESCE(r.new_snow_cm,0))::numeric AS h72_cm
                  FROM recent r
                  GROUP BY r.station_triplet
                )
                SELECT
                  COALESCE(s.name, a.station_triplet) AS station_name,
                  a.station_triplet,
                  ROUND((COALESCE(a.h24_cm,0) / 2.54)::numeric, 1) AS h24_in,
                  ROUND((COALESCE(a.h48_cm,0) / 2.54)::numeric, 1) AS h48_in,
                  ROUND((COALESCE(a.h72_cm,0) / 2.54)::numeric, 1) AS h72_in
                FROM agg a
                LEFT JOIN public.snotel_station s ON s.station_triplet = a.station_triplet
            """)
        obs_rows = _fetchall_dict(cur)


        # leaderboards for observed: top 10 by each interval
        def topn(rows, key, n=10):
            return sorted(rows, key=lambda r: (r.get(key) or 0), reverse=True)[:n]

        recent_totals = {
            "h24": _observed_with_model_forecasts(24),
            "h48": _observed_with_model_forecasts(48),
            "h72": _observed_with_model_forecasts(72),
        }

    from datetime import datetime, timedelta

    now = datetime.now()
    forecast_dates = {
        "h24": (now + timedelta(hours=24)).strftime("%b %d"),
        "h48": (now + timedelta(hours=48)).strftime("%b %d"),
        "h72": (now + timedelta(hours=72)).strftime("%b %d"),
    }


    return render(request, "dashboard/index.html", {
        "counts": counts,
        "top_forecasts": top_forecasts,
        "recent_totals": recent_totals,
        "forecast_dates": forecast_dates,
    })

def map_view(request):
    return render(request, "dashboard/map.html")



# dashboard/views.py (add imports at top)
from django.http import JsonResponse

# ... keep your existing code ...

def api_forecast_stations(request):
    """
    Returns per-station forecast totals for the next N hours as JSON:
    [
      {
        "station_triplet": "...",
        "station_name": "...",
        "lat": 0.0,
        "lon": 0.0,
        "avg_in": 0.0,
        "max_in": 0.0
      }, ...
    ]
    """
    hours_str = request.GET.get("h", "72")
    try:
        hours = int(hours_str)
        if hours not in (24, 48, 72):  # guard
            hours = 72
    except ValueError:
        hours = 72

    sql = """
    WITH latest AS (
      SELECT f.*,
             ROW_NUMBER() OVER (
               PARTITION BY COALESCE(f.station_triplet, CONCAT(f.latitude, ',', f.longitude)),
                            f.model_name,
                            f.ts_valid
               ORDER BY f.ts_forecast DESC
             ) AS rn
      FROM public.forecast_hourly f
      WHERE f.ts_valid > now()
        AND f.ts_valid <= now() + make_interval(hours => %s)
    ),
    per_model AS (
      SELECT
        COALESCE(f.station_triplet, CONCAT(f.latitude, ',', f.longitude)) AS st_key,
        f.station_triplet,
        f.model_name,
        SUM(CASE WHEN f.rn = 1 THEN COALESCE(f.snowfall_cm,0) ELSE 0 END)::numeric AS total_cm
      FROM latest f
      GROUP BY COALESCE(f.station_triplet, CONCAT(f.latitude, ',', f.longitude)),
               f.station_triplet,
               f.model_name
    ),
    by_station AS (
      SELECT
        p.st_key,
        MAX(p.station_triplet) FILTER (WHERE p.station_triplet IS NOT NULL) AS station_triplet,
        AVG(p.total_cm) AS cm_avg,
        MAX(p.total_cm) AS cm_max
      FROM per_model p
      GROUP BY p.st_key
    )
    SELECT
      COALESCE(s.station_triplet, b.st_key) AS station_triplet,
      COALESCE(s.name, b.st_key)            AS station_name,
      s.latitude                             AS lat,
      s.longitude                            AS lon,
      ROUND((COALESCE(b.cm_avg,0) / 2.54)::numeric, 1) AS avg_in,
      ROUND((COALESCE(b.cm_max,0) / 2.54)::numeric, 1) AS max_in
    FROM by_station b
    LEFT JOIN public.snotel_station s ON s.station_triplet = b.station_triplet
    WHERE s.latitude IS NOT NULL AND s.longitude IS NOT NULL
    """
    with connection.cursor() as cur:
        cur.execute(sql, [hours])
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    return JsonResponse(rows, safe=False)
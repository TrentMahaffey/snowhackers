from django.db import connection
from django.shortcuts import render
from django.core.cache import cache
from django.views.decorators.cache import cache_page
from django.conf import settings

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
    # Check cache first
    cache_key = f"obs_vs_fc_{hours_back}h"
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return cached_result

    # If not cached, run the query
    days_back = int(math.ceil(hours_back / 24))
    with connection.cursor() as cur:
        cur.execute(_OBS_VS_FC_SQL, [hours_back, days_back])
        result = _fetchall_dict(cur)

    # Cache for 5 minutes (data updates every 3 hours)
    cache.set(cache_key, result, getattr(settings, 'QUERY_CACHE_TIMEOUT', 300))
    return result



def index(request):
    # Cache counts separately (changes infrequently)
    counts = cache.get("dashboard_counts")
    if counts is None:
        with connection.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS stations FROM public.snotel_station;")
            stations = _fetchall_dict(cur)[0]["stations"]

            # Use Postgres planner's row estimate for the huge tables — exact
            # COUNT(*) on the 25GB forecast_hourly table takes minutes and
            # hangs gunicorn workers. The estimate is good enough for a
            # dashboard tile.
            cur.execute("""
                SELECT relname, reltuples::bigint AS estimate
                FROM pg_class
                WHERE relname IN ('snotel_hourly_obs', 'forecast_hourly')
            """)
            est = {r["relname"]: r["estimate"] for r in _fetchall_dict(cur)}
            hourly_obs = est.get("snotel_hourly_obs", 0)
            forecast_rows = est.get("forecast_hourly", 0)

            counts = {"stations": stations, "hourly_obs": hourly_obs, "forecast_rows": forecast_rows}
        cache.set("dashboard_counts", counts, 300)  # Cache for 5 minutes

    # Cache forecast rollup
    top_forecasts = cache.get("top_forecasts")
    if top_forecasts is None:
        with connection.cursor() as cur:
            # Query pre-computed materialized view (instant!)
            cur.execute("SELECT * FROM mv_forecast_rollup;")
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
        cache.set("top_forecasts", top_forecasts, 300)  # Cache for 5 minutes

    # Cache observed totals separately
    obs_rows_cached = cache.get("obs_rows")
    if obs_rows_cached is None:
        with connection.cursor() as cur:
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
            obs_rows_cached = _fetchall_dict(cur)
        cache.set("obs_rows", obs_rows_cached, 300)  # Cache for 5 minutes

    # Use cached observed data (not currently used in template, but keep for reference)
    # obs_rows = obs_rows_cached

    # Query from pre-computed materialized views (instant!)
    def get_obs_vs_fc(hours):
        cache_key = f"obs_vs_fc_{hours}h"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        with connection.cursor() as cur:
            cur.execute(f"SELECT * FROM mv_obs_vs_fc_{hours}h;")
            result = _fetchall_dict(cur)
        cache.set(cache_key, result, 300)
        return result

    recent_totals = {
        "h24": get_obs_vs_fc(24),
        "h48": get_obs_vs_fc(48),
        "h72": get_obs_vs_fc(72),
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


def heatmap_test(request):
    return render(request, "dashboard/heatmap_test.html")



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

    # Check cache first
    cache_key = f"api_forecast_stations_{hours}h"
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return JsonResponse(cached_result, safe=False)

    # Select appropriate columns from materialized view based on hours
    if hours == 24:
        max_col, avg_col = "h24_max_in", "h24_avg_in"
    elif hours == 48:
        max_col, avg_col = "h48_max_in", "h48_avg_in"
    else:  # 72
        max_col, avg_col = "h72_max_in", "h72_avg_in"

    sql = f"""
    SELECT
      station_triplet,
      station_name,
      latitude AS lat,
      longitude AS lon,
      {avg_col} AS avg_in,
      {max_col} AS max_in
    FROM mv_forecast_rollup
    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """
    with connection.cursor() as cur:
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Cache the result for 5 minutes
    cache.set(cache_key, rows, getattr(settings, 'QUERY_CACHE_TIMEOUT', 300))

    return JsonResponse(rows, safe=False)


def api_observed_stations(request):
    """
    Returns observed snow totals from SNOTEL stations for the past N days.
    Query param: days (default 3, max 7)
    """
    days_str = request.GET.get("days", "3")
    try:
        days = int(days_str)
        if days < 1 or days > 7:
            days = 3
    except ValueError:
        days = 3

    # Check cache
    cache_key = f"api_observed_stations_{days}d"
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return JsonResponse(cached_result, safe=False)

    sql = """
    WITH recent AS (
      SELECT a.station_triplet, a.date, a.new_snow_cm
      FROM snotel_daily_accums_cm a
      WHERE a.date >= CURRENT_DATE - make_interval(days => %s)
    ),
    totals AS (
      SELECT
        r.station_triplet,
        SUM(COALESCE(r.new_snow_cm, 0))::numeric AS total_cm
      FROM recent r
      GROUP BY r.station_triplet
    )
    SELECT
      s.station_triplet,
      s.name AS station_name,
      s.latitude AS lat,
      s.longitude AS lon,
      ROUND((COALESCE(t.total_cm, 0) / 2.54)::numeric, 1) AS total_in
    FROM snotel_station s
    LEFT JOIN totals t ON t.station_triplet = s.station_triplet
    WHERE s.latitude IS NOT NULL AND s.longitude IS NOT NULL
    """
    with connection.cursor() as cur:
        cur.execute(sql, [days])
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Cache for 5 minutes
    cache.set(cache_key, rows, getattr(settings, 'QUERY_CACHE_TIMEOUT', 300))

    return JsonResponse(rows, safe=False)


def api_regional_forecast(request):
    """
    Returns snow forecasts aggregated by geographic region.
    Groups stations into mountain ranges/regions and returns average snow per region.
    """
    hours_str = request.GET.get("h", "72")
    try:
        hours = int(hours_str)
        if hours not in (24, 48, 72):
            hours = 72
    except ValueError:
        hours = 72

    # Define regions by lat/lon boundaries
    regions = {
        "Cascades (WA)": {"lat_min": 47.0, "lat_max": 49.0, "lon_min": -122.0, "lon_max": -120.0},
        "Cascades (OR)": {"lat_min": 43.5, "lat_max": 46.5, "lon_min": -122.5, "lon_max": -121.0},
        "North Sierra": {"lat_min": 39.0, "lat_max": 40.5, "lon_min": -121.0, "lon_max": -119.5},
        "Central Sierra": {"lat_min": 37.5, "lat_max": 39.0, "lon_min": -120.0, "lon_max": -118.5},
        "South Sierra": {"lat_min": 35.5, "lat_max": 37.5, "lon_min": -119.5, "lon_max": -117.5},
        "Northern Rockies (MT)": {"lat_min": 46.0, "lat_max": 49.0, "lon_min": -115.0, "lon_max": -110.0},
        "Wyoming Ranges": {"lat_min": 42.5, "lat_max": 45.0, "lon_min": -111.0, "lon_max": -108.0},
        "Tetons/Wind River": {"lat_min": 42.5, "lat_max": 44.5, "lon_min": -111.0, "lon_max": -109.0},
        "Wasatch (UT)": {"lat_min": 40.0, "lat_max": 41.5, "lon_min": -112.5, "lon_max": -111.0},
        "Uintas (UT)": {"lat_min": 40.5, "lat_max": 41.0, "lon_min": -111.0, "lon_max": -109.5},
        "San Juans (CO)": {"lat_min": 37.0, "lat_max": 38.5, "lon_min": -108.5, "lon_max": -106.5},
        "Central CO": {"lat_min": 38.5, "lat_max": 40.0, "lon_min": -107.0, "lon_max": -105.5},
        "Front Range (CO)": {"lat_min": 39.0, "lat_max": 41.0, "lon_min": -106.0, "lon_max": -105.0},
        "North CO": {"lat_min": 40.0, "lat_max": 41.5, "lon_min": -107.0, "lon_max": -105.5},
        "Southern CO": {"lat_min": 36.5, "lat_max": 37.5, "lon_min": -107.0, "lon_max": -105.0},
        "Northern NM": {"lat_min": 35.5, "lat_max": 37.0, "lon_min": -107.5, "lon_max": -105.0},
        "Arizona": {"lat_min": 33.5, "lat_max": 36.5, "lon_min": -112.0, "lon_max": -109.0},
        "Idaho": {"lat_min": 43.0, "lat_max": 45.5, "lon_min": -116.0, "lon_max": -111.5},
    }

    metric = request.GET.get("metric", "avg_in")

    # Check cache
    cache_key = f"api_regional_forecast_{hours}h_{metric}"
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        return JsonResponse(cached_result, safe=False)

    # Get station data from materialized view
    if hours == 24:
        max_col, avg_col = "h24_max_in", "h24_avg_in"
    elif hours == 48:
        max_col, avg_col = "h48_max_in", "h48_avg_in"
    else:
        max_col, avg_col = "h72_max_in", "h72_avg_in"

    sql = f"""
    SELECT
      station_triplet,
      station_name,
      latitude,
      longitude,
      {avg_col} AS avg_in,
      {max_col} AS max_in
    FROM mv_forecast_rollup
    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """

    with connection.cursor() as cur:
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        stations = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Group stations by region and return all station points tagged with region name
    regional_data = []
    for region_name, bounds in regions.items():
        region_stations = [
            s for s in stations
            if (bounds["lat_min"] <= s["latitude"] <= bounds["lat_max"] and
                bounds["lon_min"] <= s["longitude"] <= bounds["lon_max"])
        ]

        if region_stations:
            avg_snow = sum(s[metric] for s in region_stations) / len(region_stations)
            # Calculate center point of region for label
            center_lat = (bounds["lat_min"] + bounds["lat_max"]) / 2
            center_lon = (bounds["lon_min"] + bounds["lon_max"]) / 2

            # Return all individual station points within this region
            for station in region_stations:
                regional_data.append({
                    "region": region_name,
                    "lat": station["latitude"],
                    "lon": station["longitude"],
                    "avg_in": station["avg_in"],
                    "max_in": station["max_in"],
                    "station_name": station["station_name"],
                    "is_label": False
                })

            # Add a label point at region center
            regional_data.append({
                "region": region_name,
                "lat": center_lat,
                "lon": center_lon,
                "avg_in": round(avg_snow, 1),
                "station_count": len(region_stations),
                "is_label": True  # Mark this as a label point
            })

    # Cache for 5 minutes
    cache.set(cache_key, regional_data, 300)

    return JsonResponse(regional_data, safe=False)


@cache_page(300)
def api_regional_observed(request):
    """
    Returns observed snow totals aggregated by geographic region.
    """
    days_str = request.GET.get("days", "3")
    try:
        days = int(days_str)
        if days not in (1, 2, 3, 5, 7):
            days = 3
    except ValueError:
        days = 3

    # Same regional boundaries as forecast
    regions = {
        "Cascades (WA)": {"lat_min": 47.0, "lat_max": 49.0, "lon_min": -122.0, "lon_max": -120.0},
        "Cascades (OR)": {"lat_min": 43.5, "lat_max": 46.5, "lon_min": -122.5, "lon_max": -121.0},
        "North Sierra": {"lat_min": 39.0, "lat_max": 40.5, "lon_min": -121.0, "lon_max": -119.5},
        "Central Sierra": {"lat_min": 37.5, "lat_max": 39.0, "lon_min": -120.0, "lon_max": -118.5},
        "South Sierra": {"lat_min": 35.5, "lat_max": 37.5, "lon_min": -119.5, "lon_max": -117.5},
        "Northern Rockies (MT)": {"lat_min": 46.0, "lat_max": 49.0, "lon_min": -115.0, "lon_max": -110.0},
        "Wyoming Ranges": {"lat_min": 42.5, "lat_max": 45.0, "lon_min": -111.0, "lon_max": -108.0},
        "Tetons/Wind River": {"lat_min": 42.5, "lat_max": 44.5, "lon_min": -111.0, "lon_max": -109.0},
        "Wasatch (UT)": {"lat_min": 40.0, "lat_max": 41.5, "lon_min": -112.5, "lon_max": -111.0},
        "Uintas (UT)": {"lat_min": 40.5, "lat_max": 41.0, "lon_min": -111.0, "lon_max": -109.5},
        "San Juans (CO)": {"lat_min": 37.0, "lat_max": 38.5, "lon_min": -108.5, "lon_max": -106.5},
        "Central CO": {"lat_min": 38.5, "lat_max": 40.0, "lon_min": -107.0, "lon_max": -105.5},
        "Front Range (CO)": {"lat_min": 39.0, "lat_max": 41.0, "lon_min": -106.0, "lon_max": -105.0},
        "North CO": {"lat_min": 40.0, "lat_max": 41.5, "lon_min": -107.0, "lon_max": -105.5},
        "Southern CO": {"lat_min": 36.5, "lat_max": 37.5, "lon_min": -107.0, "lon_max": -105.0},
        "Northern NM": {"lat_min": 35.5, "lat_max": 37.0, "lon_min": -107.5, "lon_max": -105.0},
        "Arizona": {"lat_min": 33.5, "lat_max": 36.5, "lon_min": -112.0, "lon_max": -109.0},
        "Idaho": {"lat_min": 43.0, "lat_max": 45.5, "lon_min": -116.0, "lon_max": -111.5},
    }

    # Get observed station data
    sql = f"""
    WITH recent AS (
        SELECT a.station_triplet, a.date, a.new_snow_cm
        FROM public.snotel_daily_accums_cm a
        WHERE a.date >= CURRENT_DATE - INTERVAL '{days} days'
    ),
    agg AS (
        SELECT
            r.station_triplet,
            SUM(COALESCE(r.new_snow_cm,0))::numeric AS total_cm
        FROM recent r
        GROUP BY r.station_triplet
    )
    SELECT
        s.station_triplet,
        s.name AS station_name,
        s.latitude,
        s.longitude,
        ROUND((COALESCE(a.total_cm,0) / 2.54)::numeric, 1) AS total_in
    FROM public.snotel_station s
    LEFT JOIN agg a ON a.station_triplet = s.station_triplet
    WHERE s.latitude IS NOT NULL AND s.longitude IS NOT NULL
    """

    with connection.cursor() as cur:
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        stations = [dict(zip(cols, r)) for r in cur.fetchall()]

    # Group stations by region
    regional_data = []
    for region_name, bounds in regions.items():
        region_stations = [
            s for s in stations
            if (bounds["lat_min"] <= s["latitude"] <= bounds["lat_max"] and
                bounds["lon_min"] <= s["longitude"] <= bounds["lon_max"])
        ]

        if region_stations:
            avg_snow = sum(s["total_in"] for s in region_stations) / len(region_stations)
            center_lat = (bounds["lat_min"] + bounds["lat_max"]) / 2
            center_lon = (bounds["lon_min"] + bounds["lon_max"]) / 2

            # Return all individual station points
            for station in region_stations:
                regional_data.append({
                    "region": region_name,
                    "lat": station["latitude"],
                    "lon": station["longitude"],
                    "total_in": station["total_in"],
                    "station_name": station["station_name"],
                    "is_label": False
                })

            # Add label point
            regional_data.append({
                "region": region_name,
                "lat": center_lat,
                "lon": center_lon,
                "total_in": round(avg_snow, 1),
                "station_count": len(region_stations),
                "is_label": True
            })

    return JsonResponse(regional_data, safe=False)

# Snow Cam Views
from pathlib import Path
from datetime import datetime
from datetime import timezone
from datetime import timedelta

def snowcams(request):
    """Snow cam timelapses viewer"""
    return render(request, "dashboard/snowcams.html")


def api_snowcam_videos(request):
    """API endpoint to list available timelapse videos"""
    # Timelapse directories with their durations and URL paths
    timelapse_dirs = {
        "daily": {"path": Path("/snowcam-timelapses"), "url_prefix": "/media/snowcams/", "label": "24 Hour"},
        "weekly": {"path": Path("/snowcam-timelapses7"), "url_prefix": "/media/snowcams7/", "label": "7 Day"},
        "monthly": {"path": Path("/snowcam-timelapses30"), "url_prefix": "/media/snowcams30/", "label": "30 Day"},
    }

    resorts = {name: prefix for prefix, name in _cam_names().items()}

    videos = []

    for duration_key, dir_info in timelapse_dirs.items():
        timelapses_dir = dir_info["path"]
        if not timelapses_dir.exists():
            continue

        for resort_name, pattern in resorts.items():
            # Find all videos for this resort
            resort_videos = sorted(
                timelapses_dir.glob(f"{pattern}_*.mp4"),
                reverse=True  # Newest first
            )

            for video_path in resort_videos[:7]:  # Last 7 entries
                filename = video_path.name
                # Extract date from filename (use END date if range, e.g., 20251031-20251130)
                try:
                    date_part = filename.split('_')[-1].split('.')[0]
                    if '-' in date_part and len(date_part) == 17:  # Date range like 20251031-20251130
                        date_str = date_part.split('-')[1][:8]  # Use end date
                    else:
                        date_str = date_part[:8]
                    date = datetime.strptime(date_str, "%Y%m%d")
                    date_display = date.strftime("%b %d, %Y")
                    date_sort = date.strftime("%Y-%m-%d")  # For filtering
                except:
                    date_display = "Unknown"
                    date_sort = "1970-01-01"

                videos.append({
                    "resort": resort_name,
                    "filename": filename,
                    "date": date_sort,  # YYYY-MM-DD for filtering
                    "date_display": date_display,  # Human-readable
                    "url": f"{dir_info['url_prefix']}{filename}",
                    "duration": duration_key,
                    "duration_label": dir_info["label"],
                })

    return JsonResponse(videos, safe=False)


def api_snowcam_predictions(request):
    """Latest model-generated snow-depth predictions per resort.

    Reads cam_predictions.json from the snowhackers root (mounted into the
    container). The file is refreshed hourly by bin/refresh_cam_predictions.sh
    which runs the fine-tuned Qwen2.5-VL model on the Blackwell.
    """
    from django.http import JsonResponse
    import json as _json
    # Look in a few candidate paths so this works in dev + container
    candidates = [
        Path("/snowcam-predictions/cam_predictions.json"),
        Path("/home/trent/snowhackers/cam_predictions.json"),
        Path(settings.BASE_DIR).parent / "cam_predictions.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = _json.loads(path.read_text())
                return JsonResponse({"predictions": data, "source": str(path)}, safe=False)
            except Exception as e:
                return JsonResponse({"error": f"failed to parse {path}: {e}"}, status=500)
    return JsonResponse({"predictions": [], "error": "cam_predictions.json not found"}, status=404)


# ---------------------------------------------------------------------------
# Image search (per-image predictions over the captured history)
# ---------------------------------------------------------------------------

# Filename pattern used to recognize the timestamp suffix on a snowcam image.
# Snowmass appends an extra _NNN millisecond field; treat it as optional.
SNOWCAM_FILENAME_RE = __import__("re").compile(
    r"^(?P<prefix>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_\d+)?\.jpg$"
)

_cams_cache = {"mtime": None, "names": {}}


def _cam_names():
    """{prefix: display name} from the cam registry (cams.json, mounted at /snowcam-config)."""
    import json as _json
    path = Path("/snowcam-config/cams.json")
    if not path.exists():
        path = Path(settings.BASE_DIR).parent / "cams.json"
    try:
        mtime = path.stat().st_mtime
        if _cams_cache["mtime"] != mtime:
            cams = _json.loads(path.read_text())["cams"]
            _cams_cache.update(mtime=mtime, names={c["prefix"]: c["name"] for c in cams})
    except (OSError, ValueError, KeyError):
        pass  # keep the last good copy
    return _cams_cache["names"]


_history_cache = {"path": None, "mtime": None, "rows": None}


def _load_predictions_history():
    """All per-image readings (model + label), newest first, from the cam_prediction table.

    Records are {prefix, ts, name, depth_inches, confidence, source, ...} (written by
    bin/history_db.py). Cached per worker until the table changes.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT count(*), max(updated_at) FROM cam_prediction")
        key = cur.fetchone()
        if _history_cache["mtime"] != key:
            # One unsorted text blob + json.loads + a Python sort: ~0.4 s cold. Per-row json casts or
            # ORDER BY inside json_agg take 1.5-2 s (Postgres sorts the wide rows).
            cur.execute("SELECT coalesce(json_agg(rec), '[]')::text FROM cam_prediction")
            rows = __import__("json").loads(cur.fetchone()[0])
            rows.sort(key=lambda r: (r.get("ts") or "", r.get("name") or ""), reverse=True)
            _history_cache.update({"path": "postgres:cam_prediction", "mtime": key, "rows": rows})
    return _history_cache["rows"], _history_cache["path"]


def snowcam_search(request):
    """Render the advanced image-search page (filters + pagination)."""
    return render(request, "dashboard/snowcam_search.html")


def snowcam_predictions_page(request):
    """Render the simple per-image predictions feed (image + model reading)."""
    return render(request, "dashboard/snowcam_predictions.html")


def api_snowcam_resorts(request):
    """List of resorts that have at least one row in the predictions history."""
    rows, _ = _load_predictions_history()
    seen = set()
    out = []
    for r in rows:
        prefix = r.get("prefix")
        if prefix and prefix not in seen:
            seen.add(prefix)
            out.append({
                "prefix": prefix,
                "label": _cam_names().get(prefix, prefix),
            })
    out.sort(key=lambda x: x["label"])
    return JsonResponse({"resorts": out, "total": len(out)})


def api_snowcam_images(request):
    """Paginated image search over the predictions history.

    Query params:
      resort:      comma-separated list of filename prefixes (e.g. "alta_snowstake,park_city")
      start_date:  YYYY-MM-DD (inclusive)
      end_date:    YYYY-MM-DD (inclusive)
      min_inches:  float; rows where depth_inches < min are dropped (rows with no
                   depth are dropped UNLESS the inches filter is fully absent)
      max_inches:  float; rows where depth_inches > max are dropped
      page:        1-based page number (default 1)
      per_page:    items per page (default 60, max 200)
      sort:        "ts_desc" (default), "ts_asc", "depth_desc", "depth_asc"
    """
    from datetime import datetime as _dt

    rows, source = _load_predictions_history()

    # --- parse filters ---
    resort_raw = request.GET.get("resort", "").strip()
    resort_filter = {r for r in (s.strip() for s in resort_raw.split(",")) if r}

    start_date = request.GET.get("start_date", "").strip()
    end_date = request.GET.get("end_date", "").strip()
    start_ts = f"{start_date} 00:00:00" if start_date else None
    end_ts = f"{end_date} 23:59:59" if end_date else None

    def _parse_float(name):
        raw = request.GET.get(name, "").strip()
        if raw == "":
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    min_inches = _parse_float("min_inches")
    max_inches = _parse_float("max_inches")
    inches_filter_active = (min_inches is not None) or (max_inches is not None)

    # Separate filter on the MODEL's predicted depth (regardless of whether the
    # row is source=label or source=model). For label rows, looks at
    # model_depth_inches; for model rows, looks at depth_inches.
    model_min = _parse_float("model_min_inches")
    model_max = _parse_float("model_max_inches")
    model_filter_active = (model_min is not None) or (model_max is not None)

    # Source filter: "all" (default), "label", "model"
    source_filter = request.GET.get("source", "all").strip() or "all"
    # Confidence filter: "all" (default), "high", "medium", "low"
    conf_filter = request.GET.get("confidence", "all").strip() or "all"
    # disagree filter: when "1", only show images where label != model prediction
    only_disagreements = request.GET.get("disagree", "").strip() in {"1", "true"}

    try:
        page = max(1, int(request.GET.get("page", "1") or "1"))
    except ValueError:
        page = 1
    try:
        per_page = max(1, min(200, int(request.GET.get("per_page", "60") or "60")))
    except ValueError:
        per_page = 60

    sort = request.GET.get("sort", "ts_desc")

    # --- filter ---
    filtered = []
    for r in rows:
        if resort_filter and r.get("prefix") not in resort_filter:
            continue
        ts = r.get("ts") or ""
        if start_ts and ts < start_ts:
            continue
        if end_ts and ts > end_ts:
            continue
        depth = r.get("depth_inches")
        if inches_filter_active:
            if depth is None:
                continue
            if min_inches is not None and depth < min_inches:
                continue
            if max_inches is not None and depth > max_inches:
                continue
        if source_filter != "all" and r.get("source") != source_filter:
            continue
        if conf_filter != "all" and r.get("confidence") != conf_filter:
            continue
        if model_filter_active:
            # Extract the model's reading: depth_inches for model rows, or
            # model_depth_inches that was carried over onto a label row.
            md = (r.get("model_depth_inches")
                  if r.get("source") == "label"
                  else r.get("depth_inches"))
            if md is None:
                continue
            if model_min is not None and md < model_min:
                continue
            if model_max is not None and md > model_max:
                continue
        if only_disagreements:
            # Only rows that have both label and model readings, where they differ.
            md = r.get("model_depth_inches")
            if r.get("source") != "label" or md is None or depth is None:
                continue
            if abs((md or 0) - (depth or 0)) < 0.5:
                continue
        filtered.append(r)

    # --- sort ---
    if sort == "ts_asc":
        filtered.sort(key=lambda r: r.get("ts") or "")
    elif sort == "depth_desc":
        filtered.sort(key=lambda r: (-(r.get("depth_inches") or -1), r.get("ts") or ""), reverse=False)
    elif sort == "depth_asc":
        filtered.sort(key=lambda r: ((r.get("depth_inches") if r.get("depth_inches") is not None else 1e9), r.get("ts") or ""))
    else:  # ts_desc default
        filtered.sort(key=lambda r: r.get("ts") or "", reverse=True)

    total = len(filtered)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = filtered[start:end]

    # --- shape response ---
    items = []
    for r in page_rows:
        prefix = r.get("prefix") or ""
        filename = r.get("name") or ""
        items.append({
            "resort_prefix": prefix,
            "resort_label": _cam_names().get(prefix, prefix),
            "ts": r.get("ts"),
            "filename": filename,
            "image_url": f"/media/snapshots/{filename}" if filename else None,
            "depth_inches": r.get("depth_inches"),
            "confidence": r.get("confidence"),
            "source": r.get("source"),  # "label" | "model" | "manual"
            "label_id": r.get("label_id"),  # numeric id from labels.json (label rows only)
            # Side-by-side: when source=="label", expose the model's prior reading
            # (set by refresh_labels_in_history.py). When source=="model" these
            # fields are absent — the model reading IS depth_inches.
            "model_depth_inches": r.get("model_depth_inches"),
            "model_confidence": r.get("model_confidence"),
        })

    return JsonResponse({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "source_file": source,
    })


def api_snowcam_depth_grid(request):
    """For each resort, find the prediction closest to each target depth.

    Targets default to 3", 6", 9", 12". For each resort × target we pick the
    single closest match within tolerance. Model predictions (source=model)
    are preferred; if no model prediction exists within tolerance for a given
    target, we fall back to hand-labeled training images (source=label) so
    newly-onboarded cams without seasonal predictions still have something to
    show.

    Query params:
      targets:   comma-separated list of target inches (default "3,6,9,12")
      tolerance: max distance from target in inches (default 1.5)
    """
    from django.http import JsonResponse

    targets_raw = request.GET.get("targets", "3,6,9,12")
    try:
        targets = [float(x) for x in targets_raw.split(",") if x.strip()]
    except ValueError:
        targets = [3.0, 6.0, 9.0, 12.0]
    try:
        tolerance = float(request.GET.get("tolerance", "1.5"))
    except ValueError:
        tolerance = 1.5

    rows, source = _load_predictions_history()

    # Group rows by resort prefix, split into model and label buckets
    model_by_resort = {}
    label_by_resort = {}
    for r in rows:
        depth = r.get("depth_inches")
        if depth is None:
            continue
        prefix = r.get("prefix")
        if not prefix:
            continue
        src = r.get("source")
        if src == "model":
            model_by_resort.setdefault(prefix, []).append(r)
        elif src == "label":
            label_by_resort.setdefault(prefix, []).append(r)

    def _closest(rows_for_resort, target):
        best = None
        best_dist = tolerance
        for r in rows_for_resort:
            dist = abs(r["depth_inches"] - target)
            if dist <= best_dist:
                best = r
                best_dist = dist
        return best

    all_prefixes = set(model_by_resort) | set(label_by_resort)
    out_resorts = []
    for prefix in all_prefixes:
        model_rows = model_by_resort.get(prefix, [])
        label_rows = label_by_resort.get(prefix, [])
        samples = []
        for target in targets:
            best = _closest(model_rows, target)
            used_source = "model"
            if best is None:
                best = _closest(label_rows, target)
                used_source = "label"
            if best is None:
                samples.append({"target": target, "match": None})
            else:
                name = best.get("name") or ""
                samples.append({
                    "target": target,
                    "match": {
                        "filename": name,
                        "image_url": f"/media/snapshots/{name}" if name else None,
                        "ts": best.get("ts"),
                        "depth_inches": best.get("depth_inches"),
                        "confidence": best.get("confidence"),
                        "source": used_source,
                    },
                })
        out_resorts.append({
            "prefix": prefix,
            "label": _cam_names().get(prefix, prefix),
            "samples": samples,
            "total_predictions": len(model_rows),
            "total_labels": len(label_rows),
        })

    out_resorts.sort(key=lambda x: x["label"])

    return JsonResponse({
        "targets": targets,
        "tolerance": tolerance,
        "resorts": out_resorts,
        "source_file": source,
    })


def snowcam_accuracy_page(request):
    """Accuracy demo: hand-labeled stake images vs model readings, per resort and depth.

    Data is baked offline into dashboard/data/accuracy_demo.json by the
    accuracy-demo eval (see snowhackers bin/ + snowcammeasurement/finetune).
    """
    import json as _json
    path = Path(__file__).resolve().parent / "data" / "accuracy_demo.json"
    try:
        data = _json.loads(path.read_text())
    except Exception:
        data = {}
    return render(request, "dashboard/snowcam_accuracy.html", {
        "overall": data.get("overall", {}),
        "test_stats": data.get("test_stats", {}),
        "resorts": data.get("resorts", []),
        "points_json": data.get("points", []),
        "model_run": data.get("model_run", ""),
        "generated_at": data.get("generated_at", ""),
    })


# ---------------------------------------------------------------------------
# Storm showcase: homepage, resort pages, curation (added 2026-10-08)
# Highlights are produced nightly by snowhackers/bin/build_highlights.py.
# ---------------------------------------------------------------------------
import os

_SHOWCASE_DATA = Path(__file__).resolve().parent / "data"
_HL_CATALOG = _SHOWCASE_DATA / "highlights.json"
_HL_OVERRIDES = _SHOWCASE_DATA / "highlight_overrides.json"
_CURATE_KEY_FILE = _SHOWCASE_DATA / "curate_key.txt"
_PREDICTIONS_FILE = Path("/snowcam-predictions/cam_predictions.json")
_TIMELAPSE_DIRS = (
    ("24h", "24 Hour", Path("/snowcam-timelapses"), "/media/snowcams/"),
    ("7d", "7 Day", Path("/snowcam-timelapses7"), "/media/snowcams7/"),
    ("30d", "30 Day", Path("/snowcam-timelapses30"), "/media/snowcams30/"),
)
_LIVE_MAX_AGE_H = 6


def _read_json(path, default):
    import json as _json
    try:
        return _json.loads(Path(path).read_text())
    except Exception:
        return default


def _current_season():
    today = datetime.now().date()
    y = today.year if today.month >= 8 else today.year - 1
    return f"{y}-{str(y + 1)[2:]}"


def _dates_label(start, end):
    """'Dec 24, 2025' / 'Dec 24–25, 2025' / 'Dec 30, 2025 – Jan 2, 2026'"""
    from datetime import date as _date
    a, b = _date.fromisoformat(start), _date.fromisoformat(end)
    if a == b:
        return f"{a:%b} {a.day}, {a.year}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{a:%b} {a.day}\u2013{b.day}, {a.year}"
    if a.year == b.year:
        return f"{a:%b} {a.day} \u2013 {b:%b} {b.day}, {a.year}"
    return f"{a:%b} {a.day}, {a.year} \u2013 {b:%b} {b.day}, {b.year}"


def _highlights():
    """Visible highlights (hidden removed), pinned first, then biggest storm."""
    catalog = _read_json(_HL_CATALOG, {})
    overrides = _read_json(_HL_OVERRIDES, {})
    items = []
    for h in catalog.get("highlights", []):
        o = overrides.get(h["id"], {})
        if o.get("hidden"):
            continue
        items.append({**h, "pinned": bool(o.get("pinned")), "dates_label": _dates_label(h["start"], h["end"])})
    items.sort(key=lambda h: (not h["pinned"], -h["storm_total_in"]))
    return items, catalog


_DAYLIGHT_CACHE = {"mtime": None, "frames": {}}


def _latest_daylight_frames():
    """{prefix: [(captured_at_utc_text, image_name), ...]} newest daylight (9am-4pm local) frames first."""
    from zoneinfo import ZoneInfo
    rows, _ = _load_predictions_history()
    mtime = _history_cache["mtime"]
    if _DAYLIGHT_CACHE["mtime"] == mtime:
        return _DAYLIGHT_CACHE["frames"]
    local = ZoneInfo("America/Denver")
    best = {}
    for r in rows:
        ts, name, prefix = r.get("ts"), r.get("name"), r.get("prefix")
        if not (ts and name and prefix) or r.get("source") != "model":
            continue
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if 9 <= dt.astimezone(local).hour <= 16:
            best.setdefault(prefix, []).append((ts, name))
    best = {p: sorted(v, reverse=True)[:12] for p, v in best.items()}
    _DAYLIGHT_CACHE.update({"mtime": mtime, "frames": best})
    return best


def _is_night(captured_at_utc_text):
    from zoneinfo import ZoneInfo
    try:
        dt = datetime.strptime(captured_at_utc_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    h = dt.astimezone(ZoneInfo("America/Denver")).hour
    return h < 7 or h >= 19


def _live_stakes():
    """Latest frame + model reading per cam, newest-and-snowiest first."""
    now = datetime.utcnow()
    stakes = []
    for p in _read_json(_PREDICTIONS_FILE, []):
        prefix = p.get("prefix")
        if not prefix:
            continue
        age_h = None
        if p.get("captured_at"):
            try:
                age_h = (now - datetime.strptime(p["captured_at"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
            except ValueError:
                pass
        live = age_h is not None and age_h <= _LIVE_MAX_AGE_H and not p.get("error")
        sherpa = p.get("stake_type") == "sherpa_bar"
        reading = p.get("snowfall_24h_inches") if sherpa else p.get("depth_inches")
        stakes.append({
            "prefix": prefix,
            "resort": p.get("resort") or _cam_names().get(prefix, prefix),
            "image_url": f"/media/snapshots/{p['image']}" if p.get("image") else None,
            "captured_at": p.get("captured_at"),
            "age_h": round(age_h, 1) if age_h is not None else None,
            "live": live,
            "reading": reading if live else None,
            "reading_label": "24h" if sherpa else "on stake",
            "confidence": p.get("confidence"),
            "stake_type": p.get("stake_type"),
        })
    daylight = _latest_daylight_frames()
    for st in stakes:
        st["display_url"], st["display_note"] = st["image_url"], None
        if not _is_night(st["captured_at"]):
            continue
        # Only a recent daylight frame that still exists on disk (thinning deletes some).
        cutoff = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
        for ts, name in daylight.get(st["prefix"], []):
            if ts < cutoff:
                break
            if os.path.exists(f"/snowcam-snapshots/{name}"):
                st["display_url"] = f"/media/snapshots/{name}"
                st["display_note"] = "last daylight frame"
                break
    stakes.sort(key=lambda s: (not s["live"], -(s["reading"] or 0), s["resort"]))
    return stakes


def home(request):
    highlights, catalog = _highlights()
    season = _current_season()
    pinned = [h for h in highlights if h["pinned"]]
    this_season = [h for h in highlights if h["season"] == season]
    hero = (pinned or sorted(this_season, key=lambda h: h["end"], reverse=True) or highlights or [None])[0]
    gallery = [h for h in highlights if not hero or h["id"] != hero["id"]]
    seasons = sorted({h["season"] for h in gallery}, reverse=True)
    stakes = _live_stakes()
    return render(request, "dashboard/home.html", {
        "hero": hero,
        "gallery": gallery,
        "seasons": seasons,
        "current_season": season,
        "stakes": stakes,
        "live_count": sum(1 for s in stakes if s["live"]),
        "cam_count": len(stakes),
        "storm_count": len(highlights),
        "generated_at": catalog.get("generated_at"),
    })


def _latest_timelapses(prefix):
    out = []
    head = prefix + "_"
    for key, label, directory, url_prefix in _TIMELAPSE_DIRS:
        try:
            names = [n for n in os.listdir(directory) if n.startswith(head) and n.endswith(".mp4")]
        except OSError:
            names = []
        if names:
            newest = max(names, key=lambda n: (n[len(head):].split("-")[-1], n))
            out.append({"key": key, "label": label, "url": url_prefix + newest})
    return out


def resort_page(request, prefix):
    from django.http import Http404
    stakes = {s["prefix"]: s for s in _live_stakes()}
    if prefix not in stakes and prefix not in _cam_names():
        raise Http404("Unknown cam")
    stake = stakes.get(prefix) or {"prefix": prefix, "resort": _cam_names().get(prefix, prefix), "live": False}
    highlights, catalog = _highlights()
    mine = [h for h in highlights if h["prefix"] == prefix]

    season = _current_season()
    start_year = int(season[:4])
    series = catalog.get("daily", {}).get(prefix, [])
    this = [r for r in series if r[0] >= f"{start_year}-08-01"]
    if not any(v > 0 for _, v in this):  # nothing yet this season: show last season instead
        chart_season = f"{start_year - 1}-{str(start_year)[2:]}"
        this = [r for r in series if f"{start_year - 1}-08-01" <= r[0] < f"{start_year}-08-01"]
    else:
        chart_season = season
    # Resort-reported 24h totals (same-day reports only) for the same window.
    reported = []
    report_name = stake["resort"].replace(" Headwaters", "")
    if this:
        try:
            with connection.cursor() as cur:
                cur.execute(
                    "SELECT report_date, snow_24h_in FROM resort_snow_reports_fresh "
                    "WHERE resort = %s AND report_date BETWEEN %s AND %s AND snow_24h_in IS NOT NULL "
                    "ORDER BY report_date",
                    [report_name, this[0][0], this[-1][0]],
                )
                reported = [[d.isoformat(), float(v)] for d, v in cur.fetchall()]
        except Exception:
            reported = []

    # Model-independent reference: daily depth gain at the nearest SNOTEL station.
    sn = catalog.get("snotel_daily", {}).get(prefix) or {}
    snotel = [r for r in sn.get("series", []) if this and this[0][0] <= r[0] <= this[-1][0]]
    return render(request, "dashboard/resort.html", {
        "stake": stake,
        "timelapses": _latest_timelapses(prefix),
        "highlights": mine,
        "chart_season": chart_season,
        "fc": _resort_forecast(prefix)[0],
        "fc_zone": _resort_forecast(prefix)[1],
        "snotel_station": f"{sn['station']} ({sn['km']} km)" if sn else None,
        "sherpa": stake.get("stake_type") == "sherpa_bar",
        "chart_data": {"model": this, "reported": reported, "snotel": snotel,
                       "sherpa": stake.get("stake_type") == "sherpa_bar"},
    })


def _curate_key():
    try:
        return _CURATE_KEY_FILE.read_text().strip()
    except OSError:
        return ""


def curate_page(request):
    _, catalog = _highlights()
    overrides = _read_json(_HL_OVERRIDES, {})
    selected = [{**h, "pinned": bool(overrides.get(h["id"], {}).get("pinned")),
                 "hidden": bool(overrides.get(h["id"], {}).get("hidden")),
                 "dates_label": _dates_label(h["start"], h["end"])} for h in catalog.get("highlights", [])]
    for c in catalog.get("candidates", []):
        c["dates_label"] = _dates_label(c["start"], c["end"])
    candidates = catalog.get("candidates", [])
    hidden = [c for c in candidates if overrides.get(c["id"], {}).get("hidden")]
    others = [c for c in candidates if not overrides.get(c["id"], {}).get("hidden")]
    return render(request, "dashboard/curate.html", {
        "selected": selected, "hidden": hidden, "others": others[:60],
        "pinned_pending": [k for k, v in overrides.items() if v.get("pinned") and k not in {h["id"] for h in selected}],
    })


def api_curate(request):
    """POST {id, action: pin|unpin|hide|unhide, key} -> updates highlight_overrides.json."""
    import hmac
    import json as _json
    from django.http import JsonResponse
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)
    try:
        body = _json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "bad json"}, status=400)
    expected = _curate_key()
    if not expected or not hmac.compare_digest(str(body.get("key", "")), expected):
        return JsonResponse({"error": "bad key"}, status=403)
    hid, action = str(body.get("id", "")), body.get("action")
    if not hid or action not in ("pin", "unpin", "hide", "unhide"):
        return JsonResponse({"error": "need id and action"}, status=400)
    overrides = _read_json(_HL_OVERRIDES, {})
    entry = overrides.get(hid, {})
    if action in ("pin", "unpin"):
        entry["pinned"] = action == "pin"
        if entry["pinned"]:
            entry["hidden"] = False
    else:
        entry["hidden"] = action == "hide"
        if entry["hidden"]:
            entry["pinned"] = False
    entry = {k: v for k, v in entry.items() if v}
    if entry:
        overrides[hid] = entry
    else:
        overrides.pop(hid, None)
    tmp = _HL_OVERRIDES.with_suffix(".tmp")
    tmp.write_text(_json.dumps(overrides, indent=1, sort_keys=True))
    os.replace(tmp, _HL_OVERRIDES)
    return JsonResponse({"ok": True, "id": hid, "state": entry})


# ---------------------------------------------------------------------------
# Snow forecast (added 2026-10-12). Data: bin/build_forecast.py -> data/forecast.json
# ---------------------------------------------------------------------------
_FORECAST_FILE = _SHOWCASE_DATA / "forecast.json"


def _forecast():
    return _read_json(_FORECAST_FILE, {})


def _fmt_range(r):
    lo, hi = r.get("low", 0), r.get("high", 0)
    if hi < 1:
        return "a dusting" if hi >= 0.3 else "0″"
    lo_i, hi_i = round(lo), round(hi)
    return f"{hi_i}″" if lo_i == hi_i else f"{lo_i}–{hi_i}″"


def _decorate_forecast(f):
    f["r24"], f["r72"], f["r7d"] = _fmt_range(f["24h"]), _fmt_range(f["72h"]), _fmt_range(f["7d"])
    peak = max([d["high"] for d in f["daily"]] + [1])
    for d in f["daily"]:
        dt = datetime.fromisoformat(d["date"])
        d["dow"] = dt.strftime("%a")
        d["mean_pct"] = round(100 * d["mean"] / peak)
        d["high_pct"] = round(100 * d["high"] / peak)
        d["label"] = _fmt_range(d)
    return f


def forecast_page(request):
    data = _forecast()
    zones = data.get("zones", [])
    for z in zones:
        _decorate_forecast(z["forecast"])
        for r in z["resorts"]:
            _decorate_forecast(r["forecast"])
    zones.sort(key=lambda z: -z["forecast"]["7d"]["mean"])
    points = []
    for z in zones:
        wx = z.get("weather") or []
        for d, w in zip(z["forecast"]["daily"], wx):
            d["tmax_f"], d["tmin_f"], d["snow_level_ft"] = w.get("tmax_f"), w.get("tmin_f"), w.get("snow_level_ft")
            d["rain"] = (w.get("rain_hours") or 0) > 0
        for r in z["resorts"]:
            if r.get("coords"):
                points.append({"lat": r["coords"][0], "lon": r["coords"][1], "name": r["resort"], "zone": z["id"],
                               "zone_name": z["name"], "mean": r["forecast"]["7d"]["mean"], "label": r["forecast"]["r7d"]})
    return render(request, "dashboard/forecast.html", {
        "zones": zones, "generated_at": data.get("generated_at"), "llm_model": data.get("llm_model"),
        "map_points": points,
    })


def _resort_forecast(prefix):
    for z in _forecast().get("zones", []):
        for r in z["resorts"]:
            if r["prefix"] == prefix:
                return _decorate_forecast(r["forecast"]), z
    return None, None


def _forecast_strip(n=4):
    zones = _forecast().get("zones", [])
    zones = sorted(zones, key=lambda z: -z["forecast"]["7d"]["mean"])[:n]
    return [{"id": z["id"], "name": z["name"], "headline": z["text"]["headline"],
             "r7d": _fmt_range(z["forecast"]["7d"])} for z in zones]

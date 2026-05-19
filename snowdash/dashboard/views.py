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

            cur.execute("SELECT COUNT(*) AS hourly_obs FROM public.snotel_hourly_obs;")
            hourly_obs = _fetchall_dict(cur)[0]["hourly_obs"]

            cur.execute("SELECT COUNT(*) AS forecast_rows FROM public.forecast_hourly;")
            forecast_rows = _fetchall_dict(cur)[0]["forecast_rows"]

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

    # Resort patterns
    resorts = {
        # Colorado
        "A-Basin": "abasin",
        "Aspen Mountain": "aspen",
        "Aspen Highlands": "highlands",
        "Buttermilk": "buttermilk",
        "Snowmass": "snowmass",
        "Beaver Creek": "beavercreek_snowstake",
        "Breckenridge": "breckenridge_snowstake",
        "Copper": "copper",
        "Crested Butte": "crestedbutte_pow",
        "Eldora": "eldora",
        "Keystone": "keystone_snowstake",
        "Loveland": "loveland",
        "Monarch": "monarch",
        "Powderhorn": "powderhorn",
        "Steamboat": "steamboat_snowstake",
        "Sunlight": "sunlight_snapshot",
        "Telluride": "telluride_powcam",
        "Vail": "vail_snowsummit",
        "Winter Park": "winter_park",
        # Utah
        "Alta": "alta_snowstake",
        "Snowbird": "snowbird_snowstake",
        "Park City": "park_city",
        "Sundance": "sundance",
        "Brian Head": "brian_head",
        "Cherry Peak": "cherry_peak",
        "Powder Mountain": "powder_mountain",
        # Montana / Wyoming / Idaho
        "Big Sky": "bigsky_andesite",
        "Bridger Bowl": "bridgerbowl_redchair",
        "Discovery": "discovery_snowstake",
        "Grand Targhee": "grandtarghee",
        "Whitefish": "whitefish",
        # California / Nevada
        "Boreal": "boreal",
        "Kirkwood": "kirkwood",
        "Northstar": "northstar",
    }

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

# Reverse map: filename prefix → display name (mirrors api_snowcam_videos).
# Keep these two in sync.
# Filename pattern used to recognize the timestamp suffix on a snowcam image.
# Snowmass appends an extra _NNN millisecond field; treat it as optional.
SNOWCAM_FILENAME_RE = __import__("re").compile(
    r"^(?P<prefix>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_\d+)?\.jpg$"
)

SNOWCAM_PREFIX_TO_RESORT = {
    "abasin": "A-Basin",
    "alta_snowstake": "Alta",
    "aspen": "Aspen Mountain",
    "beavercreek_snowstake": "Beaver Creek",
    "bigsky_andesite": "Big Sky",
    "boreal": "Boreal",
    "breckenridge_snowstake": "Breckenridge",
    "bridgerbowl_redchair": "Bridger Bowl",
    "brian_head": "Brian Head",
    "buttermilk": "Buttermilk",
    "cherry_peak": "Cherry Peak",
    "copper": "Copper",
    "crestedbutte_pow": "Crested Butte",
    "discovery_snowstake": "Discovery",
    "eldora": "Eldora",
    "grandtarghee": "Grand Targhee",
    "highlands": "Aspen Highlands",
    "keystone_snowstake": "Keystone",
    "kirkwood": "Kirkwood",
    "loveland": "Loveland",
    "monarch": "Monarch",
    "northstar": "Northstar",
    "park_city": "Park City",
    "powder_mountain": "Powder Mountain",
    "powderhorn": "Powderhorn",
    "snowbird_snowstake": "Snowbird",
    "snowmass": "Snowmass",
    "steamboat_snowstake": "Steamboat",
    "sundance": "Sundance",
    "sunlight_snapshot": "Sunlight",
    "telluride_powcam": "Telluride",
    "vail_snowsummit": "Vail",
    "whitefish": "Whitefish",
    "winter_park": "Winter Park",
}


_history_cache = {"path": None, "mtime": None, "rows": None}


def _load_predictions_history():
    """Load + cache cam_predictions_history.json.

    File is a flat list of records: {prefix, ts, name, depth_inches, confidence, source}.
    Cache invalidated when the file's mtime changes.
    """
    import json as _json
    candidates = [
        Path("/snowcam-predictions/cam_predictions_history.json"),
        Path("/home/trent/snowhackers/cam_predictions_history.json"),
        Path(settings.BASE_DIR).parent / "cam_predictions_history.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        mtime = path.stat().st_mtime
        if _history_cache["path"] == str(path) and _history_cache["mtime"] == mtime:
            return _history_cache["rows"], str(path)
        try:
            rows = _json.loads(path.read_text())
        except Exception:
            return [], str(path)
        if not isinstance(rows, list):
            rows = []
        _history_cache.update({"path": str(path), "mtime": mtime, "rows": rows})
        return rows, str(path)
    return [], None


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
                "label": SNOWCAM_PREFIX_TO_RESORT.get(prefix, prefix),
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
            "resort_label": SNOWCAM_PREFIX_TO_RESORT.get(prefix, prefix),
            "ts": r.get("ts"),
            "filename": filename,
            "image_url": f"/media/snapshots/{filename}" if filename else None,
            "depth_inches": r.get("depth_inches"),
            "confidence": r.get("confidence"),
            "source": r.get("source"),  # "label" | "model" | "manual"
        })

    return JsonResponse({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "source_file": source,
    })

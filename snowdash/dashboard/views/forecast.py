"""Zone snow forecast page and the forecast snippets used on other pages."""

from datetime import datetime

from django.shortcuts import render

from .common import _SHOWCASE_DATA
from .common import _read_json


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

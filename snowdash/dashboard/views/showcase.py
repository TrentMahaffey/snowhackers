"""Storm showcase: homepage, resort pages, curation."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from django.db import connection
from django.shortcuts import render

from .common import _SHOWCASE_DATA
from .common import _read_json
from .snowcams import _load_predictions_history
from .snowcams import _history_cache
from .snowcams import _cam_names
from .forecast import _forecast_strip, _resort_forecast


# ---------------------------------------------------------------------------
# Storm showcase: homepage, resort pages, curation (added 2026-10-08)
# Highlights are produced nightly by snowhackers/bin/build_highlights.py.
# ---------------------------------------------------------------------------

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
        "forecast_strip": _forecast_strip(),
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

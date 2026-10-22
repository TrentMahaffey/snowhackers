#!/usr/bin/env python3
"""Find storms in the snow-stake model readings and render highlight timelapses.

Runs nightly on the host (after the 7am timelapse jobs):
  1. Score each resort-day's NEW snow from the model readings in the cam_prediction table
     (see score_days). A reading must persist ~3 hours to count, so single-frame false
     positives (e.g. Copper's 10" on 2026-10-02) score 0.
  2. Merge consecutive snowy days into storm events; rank by storm total.
  3. Select: top storms per season (per-resort cap), every storm in the last 14 days, and
     anything pinned in highlight_overrides.json.
  4. Render each new selection (day before -> day after the storm) with the existing
     make_timelapses_with_timestamps.py inside the snowcams container, plus a poster frame.
  5. Write the catalog the homepage reads, and protected_days.txt so frame thinning keeps
     storm days at full resolution (they can be re-rendered later).

Usage: python3 bin/build_highlights.py [--dry-run] [--rebuild ID ...]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import camreg  # noqa: E402
from snowlib import km, psql_csv  # noqa: E402
import history_db  # noqa: E402

LOCAL = ZoneInfo("America/Denver")
SNOWHACKERS = Path("/home/trent/snowhackers")
CAMS = Path("/home/trent/snowcamtimelapse")
OUT = CAMS / "out"                                   # /out inside the snowcams container
CAM_CONFIG = SNOWHACKERS / "cam_config.json"
DATA = SNOWHACKERS / "snowdash/dashboard/data"
CATALOG = DATA / "highlights.json"
OVERRIDES = DATA / "highlight_overrides.json"
MEDIA = SNOWHACKERS / "snowdash/dashboard/static/highlights"   # served at /static/highlights/
PROTECTED = CAMS / "protected_days.txt"
STAGE = OUT / "_hl_stage"
RENDER = OUT / "_hl_render"

# A day counts as snowy at 3"+ of new snow: 2" is model jitter / a Sherpa bar's first rung
# flickering (Aspen Highlands 2026-01-05..12 alternated 0/2" for 8 days = a fake 16" "storm").
SNOW_DAY_IN = 3.0
MIN_STORM_IN = 6.0       # storm total needed to be a highlight candidate
MIN_PEAK_DAY_IN = 6.0    # ...and at least one real day of snow -- worth watching as a clip
TOP_PER_SEASON = 24
MAX_PER_RESORT = 3
RECENT_DAYS = 14
MAX_FRAMES = 240         # ~30 s at 8 fps
FPS = 8


def season_of(d):
    return f"{d.year}-{str(d.year + 1)[2:]}" if d.month >= 8 else f"{d.year - 1}-{str(d.year)[2:]}"


def local_date(ts_utc_text):
    # history 'ts' is the UTC capture time parsed from the filename
    dt = datetime.strptime(ts_utc_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL).date()


def score_days(rows):
    """{prefix: {local_date: (daily_new_snow_in, [(ts, depth, name), ...])}}

    New snow on day D = the largest RISE in the stake reading during D: each reading is
    compared with the lowest reading BEFORE it, looking back to 6pm the previous evening
    (plus the last reading before that, for sparse overnight gaps). Only rises count, so:
      * melt days score 0 even when the day starts deep (Winter Park 2026-05-02: 10" at
        dawn melting to 0" by noon used to score 10" when the formula was high - low);
      * settled snow sitting for weeks scores 0;
      * daily-cleared boards score their full reading (cleared to ~0, then it snows).
    Persistence: readings are smoothed with a rolling median (5 on hourly days ~= 3 h,
    3 on last season's ~4-readings/day backfill), which removes one-frame spikes AND
    one-frame dips -- a single bogus low can't fake a rise either.
    """
    from bisect import bisect_left
    from statistics import median

    per = defaultdict(list)
    for r in rows:
        if r.get("source") != "model" or r.get("depth_inches") is None or not r.get("ts"):
            continue
        if r.get("confidence") == "low":
            continue
        dt = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if 6 <= dt.astimezone(LOCAL).month <= 9:  # summer: no snow, and the frames are gone anyway
            continue
        per[r["prefix"]].append((dt, float(r["depth_inches"]), r["ts"], r.get("name")))

    scored = {}
    for prefix, series in per.items():
        series.sort()
        times = [s[0] for s in series]
        days = defaultdict(list)
        for i, s in enumerate(series):
            days[s[0].astimezone(LOCAL).date()].append(i)
        n = len(series)
        smooth = []
        for i, s in enumerate(series):
            w = 5 if len(days[s[0].astimezone(LOCAL).date()]) >= 12 else 3
            h = w // 2
            smooth.append(median(x[1] for x in series[max(0, i - h):min(n, i + h + 1)]))

        scored[prefix] = {}
        for d, idx in days.items():
            start = datetime.combine(d - timedelta(days=1), datetime.min.time(), LOCAL).replace(hour=18)
            i0 = bisect_left(times, start.astimezone(timezone.utc))
            best = 0.0
            in_day = set(idx)
            running = smooth[i0 - 1] if i0 > 0 else None  # last reading before the window
            for i in range(i0, idx[-1] + 1):
                if i in in_day and running is not None:
                    best = max(best, smooth[i] - running)
                running = smooth[i] if running is None else min(running, smooth[i])
            scored[prefix][d] = (best, [(series[i][2], series[i][1], series[i][3]) for i in idx])
    return scored


def find_storms(scored):
    storms = []
    for prefix, days in scored.items():
        snowy = sorted(d for d, (v, _) in days.items() if v >= SNOW_DAY_IN)
        run = []
        for d in snowy + [None]:
            if run and (d is None or (d - run[-1]).days > 1):
                total = sum(days[x][0] for x in run)
                peak_day = max(run, key=lambda x: days[x][0])
                if total >= MIN_STORM_IN and days[peak_day][0] >= MIN_PEAK_DAY_IN:
                    storms.append({
                        "id": f"{prefix}_{run[0]:%Y%m%d}-{run[-1]:%Y%m%d}",
                        "prefix": prefix,
                        "start": run[0].isoformat(),
                        "end": run[-1].isoformat(),
                        "days": len(run),
                        "storm_total_in": round(total, 1),
                        "max_day_in": round(days[peak_day][0], 1),
                        "peak_day": peak_day.isoformat(),
                        "season": season_of(run[0]),
                        "_peak_readings": days[peak_day][1],
                    })
                run = []
            if d is not None:
                run.append(d)
    return storms


# Approximate stake-cam coordinates (cams.json), for finding the nearest SNOTEL stations.
CAM_COORDS = camreg.coords()
SNOTEL_MAX_KM = 40
SNOTEL_MIN_DEPTH_IN = 2.0   # a nearby station must gain this much snow depth...
SNOTEL_MIN_SWE_IN = 0.2     # ...or this much water (SWE) over the storm window


def snotel_confirm(storms):
    """Annotate each storm with whether nearby SNOTEL stations saw snow during it.

    Catches model hallucinations where nothing fell (Powder Mountain 2026-01-27: cam 17",
    stations +0"). It cannot catch storms that really happened but didn't land visibly on
    the stake (Steamboat 2025-12-07..09: rime ice on the stake, stations +1.2-1.5" SWE) --
    those are what /curate/ Hide is for.
    """
    try:
        stations = [(t, n, float(la), float(lo)) for t, n, la, lo in
                    psql_csv("select station_triplet, name, latitude, longitude from snotel_station where latitude is not null")]
    except Exception as e:
        print(f"  [snotel] unavailable ({e}); storms left unconfirmed")
        return {}, {}
    near = {}
    for prefix, c in CAM_COORDS.items():
        ds = sorted((km(c, (la, lo)), t, n) for t, n, la, lo in stations)
        near[prefix] = [x for x in ds[:2] if x[0] <= SNOTEL_MAX_KM]
    triplets = sorted({t for v in near.values() for _, t, _ in v})
    if not storms or not triplets:
        return near, {}
    today = datetime.now(LOCAL).date()
    season_start = date(today.year if today.month >= 8 else today.year - 1, 8, 1)
    d0 = min([date.fromisoformat(s["start"]) - timedelta(days=1) for s in storms] + [date(season_start.year - 1, 8, 1)])
    d1 = today
    obs = defaultdict(dict)
    quoted = ",".join("'" + t.replace("'", "") + "'" for t in triplets)
    for t, d, dep, swe in psql_csv(f"select station_triplet, date, snow_depth_cm, swe_cm from snotel_daily_obs "
                                    f"where station_triplet in ({quoted}) and date between '{d0}' and '{d1}'"):
        obs[t][date.fromisoformat(d)] = (float(dep) / 2.54 if dep else None, float(swe) / 2.54 if swe else None)
    for s in storms:
        stations_near = near.get(s["prefix"], [])
        a, b = date.fromisoformat(s["start"]) - timedelta(days=1), date.fromisoformat(s["end"]) + timedelta(days=1)
        evidence, confirmed = [], None
        for dist, t, name in stations_near:
            days = [obs[t][d] for d in sorted(obs[t]) if a <= d <= b]
            if len(days) < 2:
                continue
            base_dep, base_swe = days[0]
            dg = max((x[0] for x in days if x[0] is not None), default=None)
            sg = max((x[1] for x in days if x[1] is not None), default=None)
            dg = dg - base_dep if dg is not None and base_dep is not None else None
            sg = sg - base_swe if sg is not None and base_swe is not None else None
            if dg is None and sg is None:
                continue
            hit = (dg or 0) >= SNOTEL_MIN_DEPTH_IN or (sg or 0) >= SNOTEL_MIN_SWE_IN
            confirmed = bool(confirmed) or hit
            evidence.append({"station": name, "km": round(dist), "depth_gain_in": round(dg or 0, 1), "swe_gain_in": round(sg or 0, 2)})
        s["snotel_confirmed"] = confirmed          # None = no nearby station / no data
        s["snotel"] = evidence
    return near, obs


def select(storms, overrides, today):
    hidden = {k for k, v in overrides.items() if v.get("hidden")}
    pinned = {k for k, v in overrides.items() if v.get("pinned")}
    chosen = {}
    by_season = defaultdict(list)
    for s in storms:
        by_season[s["season"]].append(s)
    for season, items in by_season.items():
        per_resort = defaultdict(int)
        # Ties at the cutoff are common (whole-inch readings); break them by the biggest
        # single day, then id, so the gallery does not reshuffle between runs.
        for s in sorted(items, key=lambda s: (-s["storm_total_in"], -s["max_day_in"], s["id"])):
            if s["id"] in hidden or s.get("snotel_confirmed") is False:
                continue
            if per_resort[s["prefix"]] >= MAX_PER_RESORT or sum(1 for c in chosen.values() if c["season"] == season) >= TOP_PER_SEASON:
                continue
            per_resort[s["prefix"]] += 1
            chosen[s["id"]] = s
    for s in storms:
        recent = (today - date.fromisoformat(s["end"])).days <= RECENT_DAYS
        unconfirmed = s.get("snotel_confirmed") is False
        if (recent and s["id"] not in hidden and not unconfirmed) or s["id"] in pinned:
            chosen[s["id"]] = s
    return chosen


def frames_for(prefix, start, end):
    """Capture files from noon the day before the storm to noon the day after."""
    lo = datetime.combine(start - timedelta(days=1), datetime.min.time(), LOCAL).replace(hour=12)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), LOCAL).replace(hour=12)
    lo_s, hi_s = lo.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S"), hi.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
    names = []
    head = prefix + "_"
    with os.scandir(OUT) as it:
        for e in it:
            n = e.name
            if not n.startswith(head) or not n.endswith(".jpg"):
                continue
            stamp = n[len(head):len(head) + 15]
            if len(stamp) == 15 and stamp[8] == "_" and lo_s <= stamp <= hi_s:
                names.append(n)
    names.sort()
    if len(names) > MAX_FRAMES:
        step = len(names) / MAX_FRAMES
        names = [names[int(i * step)] for i in range(MAX_FRAMES)]
    return names


def pick_poster(storm, available):
    """The frame that shows the storm: the peak-day reading with the most snow.

    A daytime frame is preferred only when it is within 80% of the peak -- many boards are
    cleared each morning, so after an overnight storm the daytime frames show a bare board
    (Copper 2025-12-27, Beaver Creek 2026-01-23) and the night IR frame is the real picture.
    """
    have = set(available)
    readings = [(ts, depth, name) for ts, depth, name in storm["_peak_readings"] if name in have]
    if not readings:
        return available[len(available) // 2]
    peak = max(d for _, d, _ in readings)

    def daytime(ts):
        hour = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(LOCAL).hour
        return 9 <= hour <= 16

    day = [r for r in readings if daytime(r[0]) and r[1] >= 0.8 * peak]
    return max(day or readings, key=lambda r: r[1])[2]


def render(storm, frames):
    sid = storm["id"]
    stage, rdir = STAGE / sid, RENDER / sid
    for d in (stage, rdir):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    for n in frames:  # relative links resolve both on the host and inside the container
        (stage / n).symlink_to(Path("../..") / n)
    # nice 19: rendering must never starve the 5-minute capture sweep on this box.
    cmd = ["docker", "exec", "snowcams", "nice", "-n", "19", "python3", "/app/make_timelapses_with_timestamps.py",
           "--in", f"/out/_hl_stage/{sid}", "--out", f"/out/_hl_render/{sid}",
           "--fps", str(FPS), "--interval", "5", "--crf", "28",
           "--timestamps", "--timestamp-position", "bottom-right", "--timestamp-size", "28",
           "--watermark", "snowhackers.com", "--watermark-position", "bottom-left", "--watermark-opacity", "0.5"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    produced = sorted(rdir.glob("*.mp4"))
    if not produced:
        raise RuntimeError(f"{sid}: renderer produced no video")
    MEDIA.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(produced[0], MEDIA / f"{sid}.mp4")
    shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(rdir, ignore_errors=True)


def make_poster(sid, frame_name):
    from PIL import Image
    with Image.open(OUT / frame_name) as im:
        im = im.convert("RGB")
        im.thumbnail((960, 540))
        im.save(MEDIA / f"{sid}.jpg", "JPEG", quality=82, optimize=True)


def main():
    ap = argparse.ArgumentParser(description="Find storms and render highlight timelapses")
    ap.add_argument("--dry-run", action="store_true", help="score and select only; render nothing")
    ap.add_argument("--rebuild", nargs="*", default=[], help="force re-render of these highlight ids")
    ap.add_argument("--refresh-posters", action="store_true", help="re-pick poster frames for existing clips (no video render)")
    args = ap.parse_args()

    resort_names = {e["prefix"]: e["resort"] for e in json.loads(CAM_CONFIG.read_text())}
    overrides = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    old = {h["id"]: h for h in json.loads(CATALOG.read_text())["highlights"]} if CATALOG.exists() else {}

    scored = score_days(history_db.load())
    storms = find_storms(scored)
    near, obs = snotel_confirm(storms)
    # Daily new snow at the nearest SNOTEL station (positive day-over-day depth change), the
    # model-independent reference line on each resort-page chart.
    snotel_daily = {}
    for prefix, stations_near in near.items():
        if not stations_near or stations_near[0][1] not in obs:
            continue
        dist, trip, name = stations_near[0]
        days = sorted(obs[trip].items())
        series = []
        for (da, (dep_a, _)), (db, (dep_b, _)) in zip(days, days[1:]):
            if dep_a is not None and dep_b is not None and (db - da).days == 1 and dep_b - dep_a >= 0.5:
                series.append([db.isoformat(), round(dep_b - dep_a, 1)])
        snotel_daily[prefix] = {"station": name, "km": round(dist), "series": series}
    print(f"SNOTEL: {sum(1 for s in storms if s.get('snotel_confirmed') is False)} storms rejected (no snow at nearby stations)")
    chosen = select(storms, overrides, datetime.now(LOCAL).date())
    print(f"{len(storms)} storms found; {len(chosen)} selected")

    catalog = []
    for sid, s in sorted(chosen.items(), key=lambda kv: -kv[1]["storm_total_in"]):
        start, end = date.fromisoformat(s["start"]), date.fromisoformat(s["end"])
        have_media = (MEDIA / f"{sid}.mp4").exists() and (MEDIA / f"{sid}.jpg").exists()
        # A storm still within the recent window may keep growing; re-render it each night.
        recent = (datetime.now(LOCAL).date() - end).days <= 1
        if args.dry_run:
            print(f"  {sid:44s} {s['storm_total_in']:5.1f}\" over {s['days']}d  max day {s['max_day_in']}\"  snotel={s.get('snotel_confirmed')}  "
                  f"{'(rendered)' if have_media else ''}")
            continue
        if not have_media or recent or sid in args.rebuild:
            frames = frames_for(s["prefix"], start, end)
            if len(frames) < 12:
                print(f"  skip {sid}: only {len(frames)} frames on disk")
                continue
            try:
                render(s, frames)
                make_poster(sid, pick_poster(s, frames))
                print(f"  rendered {sid} ({len(frames)} frames)")
            except Exception as e:
                print(f"  FAILED {sid}: {e}")
                continue
            n_frames = len(frames)
        else:
            n_frames = old.get(sid, {}).get("frames")
            if args.refresh_posters:
                frames = frames_for(s["prefix"], start, end)
                if frames:
                    make_poster(sid, pick_poster(s, frames))
        entry = {k: v for k, v in s.items() if not k.startswith("_")}
        entry.update({
            "resort": resort_names.get(s["prefix"], s["prefix"]),
            "video": f"/static/highlights/{sid}.mp4",
            "poster": f"/static/highlights/{sid}.jpg",
            "frames": n_frames,
            "pinned": bool(overrides.get(sid, {}).get("pinned")),
        })
        catalog.append(entry)

    if args.dry_run:
        return

    # Storms that didn't make the cut (or are hidden), for the curation page to pin/unhide.
    shown = {h["id"] for h in catalog}
    candidates = [
        {k: v for k, v in s.items() if not k.startswith("_")} | {
            "resort": resort_names.get(s["prefix"], s["prefix"]),
            "hidden": bool(overrides.get(s["id"], {}).get("hidden")),
        }
        for s in sorted(storms, key=lambda s: -s["storm_total_in"])
        if s["id"] not in shown
    ][:120]

    # Daily new-snow series per cam for the resort-page charts.
    daily = {
        prefix: [[d.isoformat(), round(v, 1)] for d, (v, _) in sorted(days.items())]
        for prefix, days in scored.items()
    }

    DATA.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG.with_suffix(".tmp")
    tmp.write_text(json.dumps({"generated_at": datetime.now(LOCAL).isoformat(timespec="seconds"),
                               "highlights": catalog, "candidates": candidates, "daily": daily, "snotel_daily": snotel_daily}))
    os.replace(tmp, CATALOG)

    # Drop media for highlights no longer selected (they can always be re-rendered).
    keep = {h["id"] for h in catalog}
    for f in MEDIA.glob("*"):
        if f.stem not in keep:
            f.unlink()

    # Storm days (+ the day either side) keep full-resolution frames.
    days = set()
    for h in catalog:
        d0, d1 = date.fromisoformat(h["start"]) - timedelta(days=1), date.fromisoformat(h["end"]) + timedelta(days=1)
        while d0 <= d1:
            days.add(f"{h['prefix']}_{d0:%Y%m%d}")
            d0 += timedelta(days=1)
    PROTECTED.write_text("".join(f"{k}\n" for k in sorted(days)))
    print(f"catalog: {len(catalog)} highlights; protected days: {len(days)}")


if __name__ == "__main__":
    main()

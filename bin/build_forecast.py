#!/usr/bin/env python3
"""Zone snow forecasts for snowhackers.com: deterministic numbers + LLM-written discussion.

Runs nightly on the .110 host after bin/update_snow.sh (02:07) has pulled GFS/ICON/ECMWF:
  1. Each resort (stake cam) gets its 2 nearest SNOTEL stations (<=40 km); each zone is a group
     of resorts. For every station the LATEST run per model is used (run timestamps vary by
     station), summed into local-day snowfall for the next 7 days.
  2. Per resort and per zone: 24h / 72h / 7-day totals for each model, the ensemble mean and
     the model range, plus a deterministic confidence from model agreement.
  3. Recent observations (SNOTEL depth change, stake cams, same-day resort reports) are attached.
  4. A local LLM (Qwen3 32B via Ollama on the Blackwell, reached over SSH because Ollama only
     listens on localhost there) writes a headline + short discussion per zone from those facts.
     A verifier checks every inch figure in the text against the facts; on mismatch the LLM is
     told what was wrong and retries, and after 3 failures a plain template text is used.
     The page always shows the computed numbers, so prose can never be the only source.
  5. Writes snowdash/dashboard/data/forecast.json (+ a dated archive copy for later scoring).

Usage: python3 bin/build_forecast.py [--no-llm] [--zones co_i70,ut_cottonwoods]
"""
import argparse
import csv
import io
import json
import math
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import camreg  # noqa: E402
from build_highlights import CAM_COORDS  # noqa: E402

LOCAL = ZoneInfo("America/Denver")
SH = Path("/home/trent/snowhackers")
DATA = SH / "snowdash/dashboard/data"
OUT = DATA / "forecast.json"
ARCHIVE = DATA / "forecast_archive"
CAM_CONFIG = SH / "cam_config.json"
PREDICTIONS = SH / "cam_predictions.json"

BW = "trent-mahaffey@192.168.0.117"
LLM_MODEL = "qwen3:32b"
LLM_MIN_FREE_MB = 22000      # qwen3:32b Q4 needs ~20 GB; GPU is shared with training + kedit
LLM_WAIT_MIN = 60

ZONES = camreg.zones()  # [(zone_id, name, [cam prefixes])] from cams.json
MODELS = ("gfs", "ecmwf", "icon")


# ---------------------------------------------------------------- data

def psql_csv(sql):
    out = subprocess.run(["docker", "exec", "snowhackers-db-1", "psql", "-U", "snowuser", "-d", "snow", "-At",
                          "-c", f"COPY ({sql}) TO STDOUT WITH CSV"], check=True, capture_output=True, text=True).stdout
    return list(csv.reader(io.StringIO(out)))


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def nearest_stations(stations):
    near = {}
    for prefix, c in CAM_COORDS.items():
        ds = sorted((km(c, (la, lo)), t, n) for t, n, la, lo in stations)
        near[prefix] = [(t, n, round(d)) for d, t, n in ds[:2] if d <= 40]
    return near


def station_forecasts(triplets, days):
    """{triplet: {model: [inches per local day]}} from each station's latest run per model."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=len(days) + 1)
    q = ",".join("'" + t.replace("'", "") + "'" for t in triplets)
    sql = (f"with l as (select station_triplet, model_name, max(ts_forecast) ts from forecast_hourly "
           f"where station_triplet in ({q}) and ts_valid >= now() group by 1, 2) "
           f"select f.station_triplet, f.model_name, f.ts_valid, f.snowfall_cm from forecast_hourly f "
           f"join l on l.station_triplet = f.station_triplet and l.model_name = f.model_name and l.ts = f.ts_forecast "
           f"where f.station_triplet in ({q}) and f.ts_valid >= now() and f.ts_valid < '{end.isoformat()}'")
    idx = {d: i for i, d in enumerate(days)}
    out = defaultdict(lambda: {m: [0.0] * len(days) for m in MODELS})
    for t, m, tv, cm in psql_csv(sql):
        if m not in MODELS or not cm:
            continue
        d = datetime.fromisoformat(tv).astimezone(LOCAL).date()
        if d in idx:
            out[t][m][idx[d]] += float(cm) / 2.54
    return out


def recent_obs(triplets):
    q = ",".join("'" + t.replace("'", "") + "'" for t in triplets)
    rows = psql_csv(f"select station_triplet, date, snow_depth_cm from snotel_daily_obs where station_triplet in ({q}) "
                    f"and date >= current_date - 4 order by 1, 2")
    by = defaultdict(list)
    for t, d, dep in rows:
        if dep:
            by[t].append(float(dep) / 2.54)
    return {t: round(v[-1] - min(v), 1) for t, v in by.items() if len(v) >= 2}


def resort_reports():
    try:
        rows = psql_csv("select resort, snow_24h_in from resort_snow_reports_fresh where report_date = (now() at time zone 'America/Denver')::date")
        return {r: float(v) for r, v in rows if v}
    except Exception:
        return {}


def summarize(series_by_model, days):
    """series: {model: [daily inches]} -> totals, ensemble, range, confidence, daily ensemble."""
    def tot(vals, n):
        return sum(vals[:n])
    res = {}
    for label, n in (("24h", 1), ("72h", 3), ("7d", 7)):
        per = {m: round(tot(series_by_model[m], n), 1) for m in MODELS if m in series_by_model}
        vals = list(per.values()) or [0.0]
        res[label] = {"models": per, "mean": round(sum(vals) / len(vals), 1), "low": round(min(vals), 1), "high": round(max(vals), 1)}
    daily = []
    for i, d in enumerate(days):
        v = [series_by_model[m][i] for m in MODELS if m in series_by_model]
        dayname = d.strftime("%A") + (" (today)" if i == 0 else " (tomorrow)" if i == 1 else "")
        daily.append({"date": d.isoformat(), "day": dayname, "mean": round(sum(v) / len(v), 1), "low": round(min(v), 1), "high": round(max(v), 1)})
    w = res["7d"]
    spread = w["high"] - w["low"]
    if w["high"] < 1:
        conf = "high"
    elif spread <= max(2.0, 0.35 * w["mean"]):
        conf = "high"
    elif spread <= max(4.0, 0.8 * w["mean"]):
        conf = "medium"
    else:
        conf = "low"
    res["confidence"] = conf
    res["daily"] = daily
    return res


def mean_series(station_list, fc):
    got = [fc[t] for t in station_list if t in fc]
    if not got:
        return None
    n = len(next(iter(got))[MODELS[0]])
    return {m: [sum(g[m][i] for g in got) / len(got) for i in range(n)] for m in MODELS}


def station_weather(station_meta, days):
    """{triplet: [ {tmax_f, tmin_f, precip_in, snow_level_ft, rain_hours} per local day ]} via Open-Meteo.

    Snow level = freezing level minus 1,000 ft (rule of thumb), median over hours with precipitation.
    Rain hours = precipitating hours with 2 m temperature above 34 F at the station's elevation.
    """
    import requests
    from statistics import median
    trips = list(station_meta)
    out = {}
    for i in range(0, len(trips), 50):
        chunk = trips[i:i + 50]
        r = requests.get("https://api.open-meteo.com/v1/forecast", timeout=90, params={
            "latitude": ",".join(str(station_meta[t][0]) for t in chunk),
            "longitude": ",".join(str(station_meta[t][1]) for t in chunk),
            "elevation": ",".join(str(station_meta[t][2] or "nan") for t in chunk),
            "hourly": "temperature_2m,freezing_level_height,precipitation",
            "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
            "forecast_days": len(days) + 1, "timezone": "America/Denver"})
        r.raise_for_status()
        data = r.json()
        data = data if isinstance(data, list) else [data]
        for t, d in zip(chunk, data):
            h = d["hourly"]
            to_ft = 1.0 if d.get("hourly_units", {}).get("freezing_level_height") == "ft" else 3.281
            per = {day: {"t": [], "pr": 0.0, "fl": [], "rain": 0} for day in days}
            for ts, temp, fl, pr in zip(h["time"], h["temperature_2m"], h["freezing_level_height"], h["precipitation"]):
                day = date.fromisoformat(ts[:10])
                if day not in per or temp is None:
                    continue
                b = per[day]
                b["t"].append(temp)
                if pr and pr > 0.005:
                    b["pr"] += pr
                    if fl is not None:
                        b["fl"].append(fl * to_ft - 1000)
                    if temp > 34:
                        b["rain"] += 1
            out[t] = [{"tmax_f": round(max(b["t"])) if b["t"] else None,
                       "tmin_f": round(min(b["t"])) if b["t"] else None,
                       "precip_in": round(b["pr"], 2),
                       "snow_level_ft": int(round(median(b["fl"]) / 500) * 500) if b["fl"] else None,
                       "rain_hours": b["rain"]} for b in (per[day] for day in days)]
    return out


def mean_weather(station_list, wx, days):
    got = [wx[t] for t in station_list if t in wx]
    if not got:
        return None
    res = []
    for i, d in enumerate(days):
        cells = [g[i] for g in got]
        def avg(k, rnd=0):
            v = [c[k] for c in cells if c[k] is not None]
            return round(sum(v) / len(v), rnd) if v else None
        sl = avg("snow_level_ft")
        res.append({"date": d.isoformat(), "tmax_f": int(avg("tmax_f")) if avg("tmax_f") is not None else None,
                    "tmin_f": int(avg("tmin_f")) if avg("tmin_f") is not None else None,
                    "precip_in": avg("precip_in", 2), "snow_level_ft": int(round(sl / 500) * 500) if sl else None,
                    "rain_hours": max(c["rain_hours"] for c in cells)})
    return res


# ---------------------------------------------------------------- LLM

def gpu_free_mb():
    try:
        out = subprocess.run(["ssh", BW, "nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits; "
                              "test -f /home/trent-mahaffey/snow_finetune/training.lock && echo LOCK || echo FREE"],
                             capture_output=True, text=True, timeout=30).stdout.split()
        return int(out[0]), out[1] == "LOCK"
    except Exception:
        return 0, True


def wait_for_gpu():
    deadline = time.time() + LLM_WAIT_MIN * 60
    while time.time() < deadline:
        free, locked = gpu_free_mb()
        if not locked and free >= LLM_MIN_FREE_MB:
            return True
        print(f"  [llm] waiting for GPU (free {free} MB, training lock {locked})")
        time.sleep(60)
    return False


def llm_chat(messages, keep_alive="10m"):
    body = json.dumps({"model": LLM_MODEL, "stream": False, "think": False, "keep_alive": keep_alive,
                       "format": "json", "messages": messages, "options": {"temperature": 0.4, "num_ctx": 8192}})
    out = subprocess.run(["ssh", BW, "curl -s -m 600 http://localhost:11434/api/chat -d @-"],
                         input=body, capture_output=True, text=True, timeout=700).stdout
    return json.loads(json.loads(out)["message"]["content"])


SYSTEM = """You are a mountain snow forecaster writing short daily forecast discussions for skiers, in the
plain, upbeat-but-honest style of a good ski-town forecaster. You are given computed model data as JSON.

Rules:
- Use ONLY numbers that appear in the data. Never invent amounts, dates, temperatures or snow levels.
- Snow amounts are inches. Prefer ranges built from the data's "low" and "high" values (e.g. "4-9 inches").
- Say when the snow falls (day names from the daily list) and how sure we are, using the given confidence.
- If little or no snow is forecast (7-day high under 1 inch), say so briefly and plainly.
- Mention recent observed snow only if the data includes it.
- Amounts under 1 inch: say "a dusting" or "a trace" instead of decimals like 0.2-0.6 inches.
- Round amounts of 1 inch or more to whole inches.
- Headline in sentence case (only the first word and proper names capitalized).
- weather_daily gives high/low temperature (F) and snow level (ft) per day. You may mention snow level (e.g. "snow level near 8,500 ft") and say rain is possible lower down when rain_hours > 0. Use only those numbers.
- last_season_model_skill says which weather model verified best in this zone and how biased the models were (negative = models under-forecast). If the bias is below -0.5, you may say models have tended to run low here. Name the best model only if helpful; don't overstate it.
- No hype words like "epic" or "dump" unless the 7-day mean is at least 12 inches.

Reply with JSON only: {"headline": "<= 12 words", "discussion": "2-4 sentences, <= 90 words", "best_day": "<YYYY-MM-DD from the daily list, or null>"}"""

NUM = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(\d+(?:\.\d+)?)?\s*(?:\"|”|″|in\b|inch)")


def allowed_numbers(facts):
    vals = set()

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, (int, float)) and not isinstance(o, bool):
            vals.add(round(float(o), 1))
    walk(facts)
    return vals


FT = re.compile(r"(\d{1,2},?\d{3})\s*(?:ft|feet|foot)")
DEG = re.compile(r"(-?\d{1,3})\s*(?:°|degrees)")


def verify(text, facts):
    ok = allowed_numbers(facts)
    bad = []
    for m in FT.finditer(text):
        v = float(m.group(1).replace(',', ''))
        if not any(abs(v - a) <= 250 for a in ok):
            bad.append(m.group(0))
    for m in DEG.finditer(text):
        v = float(m.group(1))
        if not any(abs(v - a) <= 2 for a in ok):
            bad.append(m.group(0))
    for m in NUM.finditer(text):
        for g in m.groups():
            if g is None:
                continue
            v = float(g)
            if not any(abs(v - a) <= 1.0 for a in ok):
                bad.append(g)
    return bad


KEEP_CAPS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "I-70", "SNOTEL",
             "Colorado", "Utah", "Montana", "Wyoming", "California", "Tahoe", "Aspen", "Vail", "Alta", "Snowbird",
             "Steamboat", "Eldora", "Copper", "Keystone", "Breckenridge", "Loveland", "Discovery", "Whitefish",
             "Northstar", "Boreal", "Kirkwood", "Sundance", "Snowbasin", "Powderhorn", "Sunlight", "Monarch",
             "Crested", "Butte", "Big", "Sky", "Bridger", "Bowl", "Park", "City", "Deer", "Valley", "Winter",
             "Powder", "Mountain", "Grand", "Targhee", "Mesa", "Brian", "Head", "Buttermilk", "Highlands", "Snowmass",
             "Beaver", "Creek", "A-Basin", "Wasatch", "Ogden", "Front", "Range", "Elk", "Tetons", "Gunnison"}


def sentence_case(h):
    words = h.split()
    if sum(w[:1].isupper() for w in words) <= max(1, len(words) // 2):
        return h[:1].upper() + h[1:]  # already sentence-ish
    out = [words[0][:1].upper() + words[0][1:]]
    out += [w if w.strip(",.!?:") in KEEP_CAPS else w.lower() for w in words[1:]]
    return " ".join(out)


_DEC = re.compile(r"(?<![\d.])(\d+\.\d+)(?=\s*(?:-|–|to|\"|”|″|in\b|inch))|(?<=-|–)(\d+\.\d+)")


def round_inches(text):
    """Whole inches, half-up; sub-inch amounts become "a dusting" (the prompt asks; Qwen sometimes ignores it)."""
    import math
    def r(v):
        return int(math.floor(float(v) + 0.5))
    def rng(m):
        lo, hi, unit = float(m.group(1)), float(m.group(2)), m.group(3)
        if hi < 1:
            return "a dusting"
        a, b = r(lo), r(hi)
        return f"{b} {unit}" if a == b else f"{a}-{b} {unit}"
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(inches|inch|in\b|\")", rng, text)
    def single(m):
        v, unit = float(m.group(1)), m.group(2)
        return "a dusting" if v < 1 else f"{r(v)} {unit}"
    return re.sub(r"(?<![\d-])(\d+\.\d+)\s*(inches|inch|in\b|\")", single, text)


def template_text(name, facts):
    w = facts["forecast"]["7d"]
    if w["high"] < 1:
        return {"headline": "Quiet week, little or no snow expected",
                "discussion": f"The models keep {name} mostly dry for the next 7 days.", "best_day": None}
    wet = [d for d in facts["forecast"]["daily"] if d["mean"] >= 1]
    when = ", ".join(datetime.fromisoformat(d["date"]).strftime("%a") for d in wet[:3]) or "later in the week"
    return {"headline": f"{w['low']:g}-{w['high']:g}\" over the next 7 days",
            "discussion": f"Models forecast {w['low']:g} to {w['high']:g} inches over 7 days, mostly {when}. "
                          f"Confidence is {facts['forecast']['confidence']}.",
            "best_day": wet[-1]["date"] if wet else None}


def write_zone(name, facts):
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Zone: {name}\nToday: {date.today().isoformat()}\nData:\n{json.dumps(facts)}"}]
    for attempt in range(3):
        try:
            out = llm_chat(msgs)
        except Exception as e:
            print(f"  [llm] {name}: call failed ({e})")
            break
        text = f"{out.get('headline', '')} {out.get('discussion', '')}"
        bad = verify(text, facts)
        days = {d["date"] for d in facts["forecast"]["daily"]}
        if out.get("best_day") not in days:
            out["best_day"] = None
        if not bad and out.get("headline") and out.get("discussion"):
            out["headline"] = sentence_case(round_inches(out["headline"].strip().rstrip(".")))
            out["discussion"] = round_inches(out["discussion"].strip())
            out["source"] = "llm"
            out["attempts"] = attempt + 1
            return out
        print(f"  [llm] {name}: attempt {attempt + 1} used numbers not in the data: {bad}")
        msgs += [{"role": "assistant", "content": json.dumps(out)},
                 {"role": "user", "content": f"These inch figures are not in the data: {bad}. Rewrite using only numbers from the data."}]
    t = template_text(name, facts)
    t["source"] = "template"
    return t


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--zones", default="")
    args = ap.parse_args()
    only = set(filter(None, args.zones.split(",")))

    cfg = {e["prefix"]: e for e in json.loads(CAM_CONFIG.read_text())}
    srows = psql_csv("select station_triplet, name, latitude, longitude, elevation_m from snotel_station where latitude is not null")
    stations = [(t, n, float(la), float(lo)) for t, n, la, lo, _ in srows]
    station_meta = {t: (float(la), float(lo), float(el) if el else None) for t, n, la, lo, el in srows}
    near = nearest_stations(stations)
    today = datetime.now(LOCAL).date()
    days = [today + timedelta(days=i) for i in range(7)]
    zones = [z for z in ZONES if not only or z[0] in only]
    triplets = sorted({t for _, _, ps in zones for p in ps for t, _, _ in near.get(p, [])})
    t0 = time.time()
    fc = station_forecasts(triplets, days)
    obs = recent_obs(triplets)
    reports = resort_reports()
    try:
        wx = station_weather({t: station_meta[t] for t in triplets if t in station_meta}, days)
    except Exception as e:
        print(f"  [weather] Open-Meteo failed ({e}); continuing without temps/snow level")
        wx = {}
    cams = {p["prefix"]: p for p in json.loads(PREDICTIONS.read_text())} if PREDICTIONS.exists() else {}
    print(f"forecast data for {len(fc)}/{len(triplets)} stations in {time.time() - t0:.0f}s")
    skill_all = json.loads((DATA / "forecast_skill.json").read_text()).get("zones", {}) if (DATA / "forecast_skill.json").exists() else {}

    llm_ok = not args.no_llm and wait_for_gpu()
    if not args.no_llm and not llm_ok:
        print("  [llm] GPU busy for too long; using template text")

    out_zones = []
    for zid, zname, prefixes in zones:
        zst = sorted({t for p in prefixes for t, _, _ in near.get(p, [])})
        zser = mean_series(zst, fc)
        if not zser:
            print(f"  {zid}: no forecast data, skipped")
            continue
        resorts = []
        for p in prefixes:
            rs = [t for t, _, _ in near.get(p, [])]
            ser = mean_series(rs, fc)
            if not ser:
                continue
            name = cfg.get(p, {}).get("resort", p)
            cam = cams.get(p, {})
            resorts.append({
                "prefix": p, "resort": name, "forecast": summarize(ser, days),
                "weather": mean_weather(rs, wx, days), "coords": CAM_COORDS.get(p),
                "stations": [{"triplet": t, "name": n, "km": d} for t, n, d in near.get(p, [])],
                "observed": {"snotel_72h_gain_in": max((obs.get(t, 0) for t in rs), default=None),
                             "cam_reading_in": cam.get("depth_inches", cam.get("snowfall_24h_inches")),
                             "resort_24h_in": reports.get(name)},
            })
        zfore = summarize(zser, days)
        zwx = mean_weather(zst, wx, days)
        sk = skill_all.get(zid) or {}
        ens = (sk.get("models") or {}).get("ensemble") or {}
        best = sk.get("best_model")
        skill = {"best_model": best.upper() if best else None,
                 "best_model_avg_error_in": ((sk.get("models") or {}).get(best) or {}).get("mae") if best else None,
                 "models_avg_bias_in_per_snow_day": ens.get("bias"), "snow_days_scored": ens.get("n")} if sk else None
        facts = {"forecast": zfore, "weather_daily": zwx, "last_season_model_skill": skill,
                 "resorts": [{"resort": r["resort"], "24h": r["forecast"]["24h"], "72h": r["forecast"]["72h"],
                              "7d": r["forecast"]["7d"], "observed": r["observed"]} for r in resorts]}
        text = write_zone(zname, facts) if llm_ok else {**template_text(zname, facts), "source": "template"}
        print(f"  {zid:16s} 7d {zfore['7d']['low']:g}-{zfore['7d']['high']:g}\" ({zfore['confidence']})  [{text['source']}] {text['headline']}")
        out_zones.append({"id": zid, "name": zname, "forecast": zfore, "weather": zwx, "skill": skill,
                          "coords": [CAM_COORDS[p] for p in prefixes if p in CAM_COORDS], "resorts": resorts, "text": text})

    if llm_ok:  # free the shared GPU now
        try:
            llm_chat([{"role": "user", "content": "ok"}], keep_alive=0)
        except Exception:
            pass

    result = {"generated_at": datetime.now(LOCAL).isoformat(timespec="seconds"), "llm_model": LLM_MODEL if llm_ok else None,
              "days": [d.isoformat() for d in days], "zones": out_zones}
    DATA.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(OUT)
    (ARCHIVE / f"{today.isoformat()}.json").write_text(json.dumps(result))
    print(f"wrote {OUT} ({len(out_zones)} zones)")


if __name__ == "__main__":
    main()

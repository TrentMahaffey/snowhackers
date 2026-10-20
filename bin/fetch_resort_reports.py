#!/usr/bin/env python3
"""Fetch resort-published snow reports into Postgres (table resort_snow_reports).

Gives a second, independent measurement next to SNOTEL and the snow-stake cam model:
what each resort itself reports for overnight / 24h / 48h / 72h / 7-day / storm / base /
season totals. One row per (resort, local report date); later fetches that day update it.

Sources (surveyed 2026-08, see resort_snow_report_sources.json):
  alterra  - mtnpowder.com v3 feed: full reports for Alterra-owned resorts (Ikon partners
             appear in the feed with an empty SnowReport, so they use their own sources)
  vail     - Vail Resorts sites embed an FR.snowReportData JSON object in the page
  powdr    - Snowbird / Copper / Eldora "DOR" snow-reports API
  alta     - alta.com/weather embeds a window.Alta object with the conditions block
  aspen    - Aspen Snowmass WeatherDashboard feed (snow block is null off-season)
  sundance - first-party JSON API
  powmow   - Powder Mountain first-party JSON API

`resort` values match the "resort" names in cam_config.json so reports join to cams.
Resorts publish stale numbers all summer; resort_updated_at says when the resort last
touched the report, and the resort_snow_reports_fresh view keeps only same-day updates.

The Vail sites (Akamai) reject the runner container's TLS fingerprint but accept the
host's Python, so production runs in two steps (bin/update_resort_reports.sh):
  host:   python3 bin/fetch_resort_reports.py --json-out logs/resort_reports_latest.json
  runner: python -u /app/bin/fetch_resort_reports.py --load /app/logs/resort_reports_latest.json
"""
import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

DB_URL = os.getenv("DATABASE_URL", "postgresql://snowuser:changeme@db:5432/snow")
LOCAL_TZ = ZoneInfo("America/Denver")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
# Vail Resorts sites sit behind Akamai and 403 anything that doesn't look like Chrome.
BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

# Public token from a resort widget config (v4.mtnfeed.com/resorts/winter-park.json); kept out of the repo.
ALTERRA_TOKEN = os.getenv("ALTERRA_FEED_TOKEN", "")
ALTERRA_URL = f"https://mtnpowder.com/feed/v3.json?bearer_token={ALTERRA_TOKEN}"
ALTERRA_NAMES = {  # feed Name -> our resort name
    "Arapahoe Basin": "A-Basin",
    "Deer Valley": "Deer Valley",
    "Steamboat": "Steamboat",
    "Winter Park": "Winter Park",
    # Ikon partners with no dedicated source yet; empty off-season, may fill in winter.
    "Big Sky": "Big Sky",
    "Snowbasin Resort": "Snowbasin",
}

POWDR_APIS = {  # resort -> DOR snow-reports endpoint (newest first)
    "Snowbird": "https://api.snowbird.com/api/v1/dor/drupal/snow-reports?sort=date&direction=desc",
    "Copper": "https://api.coppercolorado.com/api/v1/dor/drupal/snow-reports?sort=date&direction=desc",
    "Eldora": "https://api.eldora.com/api/v1/dor/drupal/snow-reports?sort=date&direction=desc",
}

ALTA_URL = "https://www.alta.com/weather"
ASPEN_FEED = "https://www.aspensnowmass.com/AspenSnowmass/WeatherDashboard/Feed"
ASPEN_NAMES = ("Aspen Mountain", "Aspen Highlands", "Buttermilk", "Snowmass")

VAIL_PAGES = {
    "Beaver Creek": "https://www.beavercreek.com/the-mountain/mountain-conditions/snow-and-weather-report.aspx",
    "Breckenridge": "https://www.breckenridge.com/the-mountain/mountain-conditions/snow-and-weather-report.aspx",
    "Keystone": "https://www.keystoneresort.com/the-mountain/mountain-conditions/snow-and-weather-report.aspx",
    "Vail": "https://www.vail.com/the-mountain/mountain-conditions/snow-and-weather-report.aspx",
    "Park City": "https://www.parkcitymountain.com/the-mountain/mountain-conditions/snow-and-weather-report.aspx",
    "Northstar": "https://www.northstarcalifornia.com/the-mountain/mountain-conditions/snow-and-weather-report.aspx",
    "Kirkwood": "https://www.kirkwood.com/the-mountain/mountain-conditions/snow-and-weather-report.aspx",
    # Crested Butte is the one Vail site on a different path.
    "Crested Butte": "https://www.skicb.com/the-mountain/mountain-conditions/weather-report.aspx",
}

SUNDANCE_URL = "https://www.sundanceresort.com/api/mountain-information/"  # trailing slash required
POWMOW_URL = "https://powdermountain.com/api/conditions"

FIELDS = ("snow_overnight_in", "snow_24h_in", "snow_48h_in", "snow_72h_in", "snow_7d_in",
          "storm_total_in", "base_depth_in", "season_total_in")

DDL = """
CREATE TABLE IF NOT EXISTS resort_snow_reports (
    id                  bigserial PRIMARY KEY,
    resort              text        NOT NULL,
    report_date         date        NOT NULL,
    fetched_at          timestamptz NOT NULL DEFAULT now(),
    source              text        NOT NULL,
    resort_updated_at   timestamptz,
    resort_updated_text text,
    snow_overnight_in   numeric,
    snow_24h_in         numeric,
    snow_48h_in         numeric,
    snow_72h_in         numeric,
    snow_7d_in          numeric,
    storm_total_in      numeric,
    base_depth_in       numeric,
    season_total_in     numeric,
    raw                 jsonb,
    UNIQUE (resort, report_date)
);
CREATE OR REPLACE VIEW resort_snow_reports_fresh AS
SELECT * FROM resort_snow_reports
WHERE resort_updated_at IS NOT NULL
  AND (resort_updated_at AT TIME ZONE 'America/Denver')::date = report_date;
"""


def num(v):
    """Resort feeds mix ints, numeric strings, '--' and ranges like '30-45'."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace('"', "")
    if s in ("", "-", "--", "N/A", "n/a", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        m = re.match(r"^(\d+(?:\.\d+)?)", s)
        return float(m.group(1)) if m else None


def report(resort, source, updated_at=None, updated_text=None, raw=None, **values):
    row = {"resort": resort, "source": source, "resort_updated_at": updated_at,
           "resort_updated_text": updated_text, "raw": raw}
    for f in FIELDS:
        row[f] = values.get(f)
    return row


# ---------------------------------------------------------------- sources

def fetch_alterra():
    d = requests.get(ALTERRA_URL, headers={"User-Agent": UA}, timeout=120).json()
    by_name = {(r.get("Name") or "").strip(): r for r in d.get("Resorts", [])}
    rows = []
    for feed_name, resort in ALTERRA_NAMES.items():
        r = by_name.get(feed_name)
        if not r:
            print(f"  [alterra] {feed_name!r} missing from feed")
            continue
        sr = r.get("SnowReport") or {}
        areas = [sr.get(k) or {} for k in ("AllMountain", "MidMountainArea", "SummitArea", "BaseArea")]

        def pick(key):
            for a in areas:
                v = num(a.get(key))
                if v is not None:
                    return v
            return None

        updated_text = sr.get("LastUpdate")
        try:
            updated_at = datetime.strptime(updated_text, "%Y-%m-%dT%H:%M:%S%z") if updated_text else None
        except ValueError:
            updated_at = None
        base = num(sr.get("SnowBaseRangeIn"))
        rows.append(report(
            resort, "alterra", updated_at, updated_text,
            raw={k: sr.get(k) for k in ("LastUpdate", "StormTotalIn", "SnowBaseRangeIn", "SeasonTotalIn",
                                        "AllMountain", "BaseArea", "MidMountainArea", "SummitArea")},
            snow_overnight_in=pick("SinceLiftsClosedIn"),
            snow_24h_in=pick("Last24HoursIn"),
            snow_48h_in=pick("Last48HoursIn"),
            snow_72h_in=pick("Last72HoursIn"),
            snow_7d_in=pick("Last7DaysIn"),
            storm_total_in=num(sr.get("StormTotalIn")),
            base_depth_in=base if base is not None else pick("BaseIn"),
            season_total_in=num(sr.get("SeasonTotalIn")),
        ))
    return rows


_TZ_ABBR = {"MDT": -6, "MST": -7, "PDT": -7, "PST": -8}


def _parse_vail_updated(text):
    # "Updated June 23, 2026 at 4:22 PM MDT"
    if not text:
        return None
    s = text.strip()
    if s.startswith("Updated "):
        s = s[len("Updated "):]
    parts = s.rsplit(" ", 1)
    if len(parts) != 2 or parts[1] not in _TZ_ABBR:
        return None
    try:
        dt = datetime.strptime(parts[0], "%B %d, %Y at %I:%M %p")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone(timedelta(hours=_TZ_ABBR[parts[1]])))


def _embedded_object(html, marker):
    # Locate a JS object literal by fixed-string search, then let the JSON decoder find its end.
    # (No regex over the page: these pages carry very long minified lines.)
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    obj, _ = json.JSONDecoder().raw_decode(html[j:])
    return obj


def fetch_vail():
    rows = []
    session = requests.Session()
    for resort, url in VAIL_PAGES.items():
        try:
            r = session.get(url, headers=BROWSER_HEADERS, timeout=45)
            r.raise_for_status()
            d = _embedded_object(r.text, "FR.snowReportData")
            if not d:
                print(f"  [vail] {resort}: FR.snowReportData not found (HTTP {r.status_code})")
                continue

            def inches(key):
                return num((d.get(key) or {}).get("Inches"))

            text = d.get("LastUpdatedText")
            rows.append(report(
                resort, "vail", _parse_vail_updated(text), text, raw=d,
                snow_overnight_in=inches("OvernightSnowfall"),
                snow_24h_in=inches("TwentyFourHourSnowfall"),
                snow_48h_in=inches("FortyEightHourSnowfall"),
                snow_7d_in=inches("SevenDaySnowfall"),
                base_depth_in=inches("BaseDepth"),
                season_total_in=inches("CurrentSeason"),
            ))
        except Exception as e:
            print(f"  [vail] {resort}: {type(e).__name__}: {e}")
    return rows


def fetch_sundance():
    d = requests.get(SUNDANCE_URL, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30).json()
    vals = {item.get("id"): item.get("status_value") for item in d.get("snow-weather", [])}
    return [report(
        "Sundance", "sundance", raw=d.get("snow-weather"),
        snow_24h_in=num(vals.get("24hr_snow")),
        snow_48h_in=num(vals.get("48hr_snow")),
        base_depth_in=num(vals.get("base_snow")),
        season_total_in=num(vals.get("ytd_snow")),
    )]


def fetch_powmow():
    d = requests.get(POWMOW_URL, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30).json()
    cs = (d.get("conditions") or {}).get("currentSnow") or {}

    def inches(key):
        return num((cs.get(key) or {}).get("countryValue"))

    text = cs.get("lastModified")
    try:
        updated_at = datetime.fromisoformat(text) if text else None
    except ValueError:
        updated_at = None
    return [report(
        "Powder Mountain", "powmow", updated_at, text, raw=cs,
        snow_overnight_in=inches("overnight"),
        snow_24h_in=inches("freshSnowFallDepth24H"),
        snow_48h_in=inches("freshSnowFallDepth48H"),
        snow_72h_in=inches("freshSnowFallDepth72H"),
        snow_7d_in=inches("freshSnowFallDepth7D"),
        base_depth_in=inches("snowTotalDepth"),
        season_total_in=inches("snowFallDepthCompleteSeason"),
    )]


def _local(naive_text, fmt="%Y-%m-%d %H:%M:%S"):
    try:
        return datetime.strptime(naive_text, fmt).replace(tzinfo=LOCAL_TZ) if naive_text else None
    except ValueError:
        return None


def fetch_powdr():
    rows = []
    for resort, url in POWDR_APIS.items():
        try:
            items = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30).json()
            if not items:
                continue
            it = items[0]
            comp = it.get("computed") or {}
            rows.append(report(
                resort, "powdr", _local(it.get("date")), it.get("date"),
                raw={k: it.get(k) for k in ("date", "amount", "overnight_amount", "base_depth",
                                            "unit_of_measurement", "computed")},
                snow_overnight_in=num(it.get("overnight_amount")),
                snow_24h_in=num(comp.get("24_hour")),
                snow_48h_in=num(comp.get("48_hour")),
                snow_72h_in=num(comp.get("72_hour")),
                snow_7d_in=num(comp.get("7_day")),
                base_depth_in=num(it.get("base_depth")),
                season_total_in=num(comp.get("total")),
            ))
        except Exception as e:
            print(f"  [powdr] {resort}: {type(e).__name__}: {e}")
    return rows


def fetch_alta():
    r = requests.get(ALTA_URL, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    obj = _embedded_object(r.text, "window.Alta")
    c = (obj or {}).get("conditions") or {}
    if not c:
        raise RuntimeError("window.Alta.conditions not found")
    return [report(
        "Alta", "alta", _local(c.get("dated_at")), c.get("dated_at"),
        raw={k: c.get(k) for k in ("dated_at", "last12", "last24", "storm_total", "base_depth",
                                   "year_to_date", "updated_at")},
        snow_24h_in=num(c.get("last24")),
        storm_total_in=num(c.get("storm_total")),
        base_depth_in=num(c.get("base_depth")),
        season_total_in=num(c.get("year_to_date")),
    )]


def fetch_aspen():
    # The feed's weather.snow block is null all summer, so its in-season shape is unverified.
    # Map by key name defensively; raw is stored so the parser can be tightened once it fills.
    d = requests.get(ASPEN_FEED, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30).json()
    rows = []
    for m in d.get("mountains", []):
        name = (m.get("name") or m.get("title") or "").strip()
        if name not in ASPEN_NAMES:
            continue
        snow = (m.get("weather") or {}).get("snow")
        if not snow:
            continue
        print(f"  [aspen] {name} snow block: {json.dumps(snow)[:300]}")
        flat = snow if isinstance(snow, dict) else {}

        def find(*needles):
            for k, v in flat.items():
                kl = k.lower().replace("_", "").replace("-", "")
                if all(n in kl for n in needles):
                    val = num(v.get("inches") if isinstance(v, dict) else v)
                    if val is not None:
                        return val
            return None

        rows.append(report(
            name, "aspen", raw=snow,
            snow_24h_in=find("24"),
            snow_48h_in=find("48"),
            snow_7d_in=find("7"),
            base_depth_in=find("base"),
            season_total_in=find("season"),
        ))
    return rows


SOURCES = [("alterra", fetch_alterra), ("vail", fetch_vail), ("powdr", fetch_powdr),
           ("alta", fetch_alta), ("aspen", fetch_aspen),
           ("sundance", fetch_sundance), ("powmow", fetch_powmow)]


# ---------------------------------------------------------------- storage

UPSERT = f"""
INSERT INTO resort_snow_reports
    (resort, report_date, fetched_at, source, resort_updated_at, resort_updated_text,
     {", ".join(FIELDS)}, raw)
VALUES (%(resort)s, %(report_date)s, now(), %(source)s, %(resort_updated_at)s, %(resort_updated_text)s,
        {", ".join(f"%({f})s" for f in FIELDS)}, %(raw)s)
ON CONFLICT (resort, report_date) DO UPDATE SET
    fetched_at = EXCLUDED.fetched_at,
    source = EXCLUDED.source,
    resort_updated_at = EXCLUDED.resort_updated_at,
    resort_updated_text = EXCLUDED.resort_updated_text,
    {", ".join(f"{f} = EXCLUDED.{f}" for f in FIELDS)},
    raw = EXCLUDED.raw
"""


def store(rows):
    import psycopg
    from psycopg.types.json import Jsonb
    today = datetime.now(LOCAL_TZ).date()
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute(DDL)
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(UPSERT, {**row, "report_date": today, "raw": Jsonb(row["raw"])})
    return today


def fetch_all():
    sources = list(SOURCES)
    try:
        # Small independents parsed from resort HTML (Monarch, Powderhorn, Discovery, Brian Head,
        # Whitefish, Sunlight). Imported here, not at module top: html_sources imports our helpers.
        from html_sources import HTML_SOURCES
        sources += HTML_SOURCES
        # Southern Colorado / New Mexico (2026-10-19)
        from south_sources import SOUTH_SOURCES
        sources += SOUTH_SOURCES
    except ImportError as e:
        print(f"[html] sources unavailable: {e}")
    rows, failed = [], []
    for name, fn in sources:
        try:
            got = [r for r in fn() if any(r[f] is not None for f in FIELDS)]
            rows.extend(got)
            print(f"[{name}] {len(got)} resorts")
        except Exception as e:
            failed.append(name)
            print(f"[{name}] FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()
    return rows, failed


def dump_rows(rows, path):
    out = [{**r, "resort_updated_at": r["resort_updated_at"].isoformat() if r["resort_updated_at"] else None}
           for r in rows]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "rows": out}, f)
    os.replace(tmp, path)


def load_rows(path):
    with open(path) as f:
        payload = json.load(f)
    fetched = datetime.fromisoformat(payload["fetched_at"])
    if datetime.now(timezone.utc) - fetched > timedelta(hours=2):
        raise SystemExit(f"refusing to load stale fetch from {fetched.isoformat()}")
    rows = payload["rows"]
    for r in rows:
        r["resort_updated_at"] = datetime.fromisoformat(r["resort_updated_at"]) if r["resort_updated_at"] else None
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and print; don't write to the DB")
    ap.add_argument("--json-out", metavar="PATH", help="fetch and write rows to PATH instead of the DB")
    ap.add_argument("--load", metavar="PATH", help="store rows previously written with --json-out")
    args = ap.parse_args()

    if args.load:
        rows, failed = load_rows(args.load), []
    else:
        rows, failed = fetch_all()

    for r in sorted(rows, key=lambda r: r["resort"]):
        upd = r["resort_updated_at"].astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M") if r["resort_updated_at"] else "-"
        vals = " ".join(f"{f.replace('snow_', '').replace('_in', '')}={r[f]:g}" for f in FIELDS if r[f] is not None)
        print(f"  {r['resort']:16s} [{r['source']:8s}] updated {upd:16s} {vals}")

    if not rows:
        print("no reports fetched")
        sys.exit(1)
    if args.dry_run:
        print(f"dry run: {len(rows)} reports, not stored")
    elif args.json_out:
        dump_rows(rows, args.json_out)
        print(f"wrote {len(rows)} reports to {args.json_out}")
    else:
        day = store(rows)
        print(f"stored {len(rows)} reports for {day}")
    if failed:
        print("sources failed:", ", ".join(failed))
        sys.exit(2)


if __name__ == "__main__":
    main()

"""Resort-published snow reports for southern Colorado / New Mexico (plus Ski Cooper).

Drop-in sources for fetch_resort_reports.py, same conventions as html_sources.py:
standard library + requests only; pages are parsed with html.parser into text nodes
(html_sources._nodes) or located with str.find + json raw_decode. Regexes run only on short
extracted label/value strings (< 200 chars), never on raw page HTML.

Each fetch_* returns [report(...)] and raises when the numbers aren't on the page.
Each parse_* takes the page HTML (plus any side-channel text) so the label -> field mapping
can be tested against Wayback snapshots. raw["source"] records the exact label -> value text
every field came from; labels whose meaning is not certain go to raw["unmapped"].

Usage (in fetch_all, next to HTML_SOURCES):
    from south_sources import SOUTH_SOURCES
    sources += SOUTH_SOURCES
"""
import json
import re
from datetime import datetime, timezone

import requests

from fetch_resort_reports import report, num, UA, BROWSER_HEADERS, LOCAL_TZ  # noqa: F401
from html_sources import _nodes, _has, _index, _inches, _norm, _strptime, _get, _QUOTES

# ---------------------------------------------------------------- URLs

# Mountain Capital Partners (MCP) WordPress theme: same block as Brian Head.
MCP_PAGES = {
    "Purgatory": "https://www.purgatory.ski/mountain/weather-conditions-webcams/",
    "Sipapu": "https://www.sipapu.ski/the-mountain/weather-conditions-webcams/",
    "Pajarito": "https://www.pajarito.ski/weather-conditions-webcams/",
}
WOLF_CREEK_URL = "https://wolfcreekski.com/wcs-snow-report/"
TELLURIDE_URL = "https://tellurideskiresort.com/snow-report/"
# skitaos.com/snow-report 404s since the 2026 site rebuild; the numbers are on /weather.
TAOS_URL = "https://www.skitaos.com/weather"
SKI_COOPER_URL = "https://www.skicooper.com/snow-trail-report/"
SANTA_FE_URL = "https://www.skisantafe.com/conditions/snow-report"
# The e-mail-style report page the resort publishes ("View Full Report" newsletter body).
ANGEL_FIRE_URL = "https://www.angelfireresort.com/snow-report-scraper/"
RED_RIVER_URL = "https://www.redriverskiarea.com/winter/winter-mountain/snow-report/"
SKI_APACHE_URL = "https://skiapache.com/mountain-info/trail-status/"
SILVERTON_URL = "https://silvertonmountain.com/mountain/snow-report/"

# ---------------------------------------------------------------- label classification

_FIXED = {
    "storm total": "storm_total_in",
    "base depth": "base_depth_in", "base": "base_depth_in", "settled base": "base_depth_in",
    "settled base depth": "base_depth_in",
    "season total": "season_total_in", "season": "season_total_in", "ytd": "season_total_in",
    "snowfall ytd": "season_total_in", "season to date": "season_total_in",
    "overnight": "snow_overnight_in", "snow overnight": "snow_overnight_in",
    "since lifts closed": "snow_overnight_in", "new snow (since lifts closed)": "snow_overnight_in",
}
_HOURS = {"24": "snow_24h_in", "48": "snow_48h_in", "72": "snow_72h_in"}
# "24 Hr", "24 Hrs", "24 Hours", "Last 24 Hours", "Past 24 Hours", "New Snow 24 hr", "24hr"
_HOURS_RE = re.compile(r"^(?:last |past |new snow,? )?(24|48|72) ?(?:hr|hrs|hour|hours)$")
# "7 Days", "7 Day", "Last 7 Days"  (5-day / 6-day are kept in raw)
_DAYS_RE = re.compile(r"^(?:last |past )?7 ?days?$")
# MCP sites relabel this one each year: "2025-2026 Season Total"
_SEASON_RE = re.compile(r"^\d{4}[-/]\d{2,4} season total$")


def _field(label, extra=None):
    """Normalized label -> FIELDS name, or None when the meaning isn't certain."""
    s = _norm(label)
    if len(s) > 80:
        return None
    if extra and s in extra:
        return extra[s]
    if s in _FIXED:
        return _FIXED[s]
    m = _HOURS_RE.match(s)
    if m:
        return _HOURS[m.group(1)]
    if _DAYS_RE.match(s):
        return "snow_7d_in"
    if _SEASON_RE.match(s):
        return "season_total_in"
    return None


def _value(text, where):
    """_inches plus a couple of hand-entry quirks: 'o"' typed for 0 (Silverton), stray spaces."""
    s = (text or "").translate(_QUOTES).strip()
    if s.lower() in ("o", 'o"'):
        return 0.0
    if s.strip('" ') == "":  # unit with the number left blank: '"'
        return None
    return _inches(s, where)


def _classify(resort, pairs, extra=None, required=()):
    """pairs: [(label, value_text)] -> (values, source, unmapped). First occurrence wins."""
    values, source, unmapped = {}, {}, {}
    for label, value in pairs:
        f = _field(label, extra)
        if f is None:
            unmapped[label] = value
            continue
        if f in source:
            continue
        values[f] = _value(value, f"{resort} {label!r}")
        source[f] = f"{label} -> {value!r}"
    if not source:
        raise RuntimeError(f"{resort}: no snow labels recognized (saw {[l for l, _ in pairs][:20]})")
    missing = [f for f in required if f not in source]
    if missing:
        raise RuntimeError(f"{resort}: labels for {missing} not found (saw {[l for l, _ in pairs][:20]})")
    return values, source, unmapped


def _iso(text):
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)


def _grouped(html, container_cls, label_test, value_test):
    """Pair label/value text nodes that share the innermost `container_cls` element."""
    groups, order = {}, []
    for text, anc in _nodes(html):
        ci = _index(anc, container_cls)
        if ci < 0:
            continue
        key = id(anc[ci][1])
        if key not in groups:
            groups[key] = ([], [])
            order.append(key)
        inner = anc[ci + 1:]
        if label_test(inner):
            groups[key][0].append(text)
        elif value_test(inner):
            groups[key][1].append(text)
    return [(" ".join(groups[k][0]), "".join(groups[k][1])) for k in order if groups[k][0]]


# ---------------------------------------------------------------- MCP (Purgatory, Sipapu, Pajarito)

def parse_mcp(resort, html, updated_text=None):
    # <div class="m-snow-totals-table ..."><div>
    #    <div class="m-snow-totals-top">0″</div><div class="m-snow-totals-label">24 Hr</div>
    # </div>...   (rendered twice; the first table is used). Labels are free text the resort
    # edits ("24 Hrs", "6 days", "Last 5 Days", "2025-2026 Season Total"); the whole table is
    # removed from the page off-season.
    pairs, pending, first_table = [], None, None
    for text, anc in _nodes(html):
        ti = _index(anc, "m-snow-totals-table")
        if ti < 0:
            continue
        key = id(anc[ti][1])
        if first_table is None:
            first_table = key
        if key != first_table:
            break
        if _has(anc, "m-snow-totals-top", 1):
            pending = text
        elif _has(anc, "m-snow-totals-label", 1):
            pairs.append((text, pending if pending is not None else ""))
            pending = None
    if not pairs:
        raise RuntimeError(f"{resort}: snow totals table (div.m-snow-totals-table) not on the page "
                           "- MCP sites remove it off-season")
    snow = [(l, v) for l, v in pairs if _norm(l) not in ("lifts open", "trails open")]
    values, source, unmapped = _classify(resort, snow)
    # weather.json conditions_table_updated: "October 13, 2026 12:51 pm"
    updated_at = _strptime(updated_text or "", "%B %d, %Y %I:%M %p")
    return [report(resort, "mcp_html", updated_at, updated_text,
                   raw={"source": source, "unmapped": unmapped, "labels": dict(pairs)}, **values)]


def _mcp_weather_json_url(html):
    # var wh_ajax_object = {..."weatherFile":"https:\/\/www.purgatory.ski\/wp-content\/uploads\/sites\/2\/m-json\/weather.json",...}
    i = html.find('"weatherFile":"')
    if i < 0:
        return None
    i += len('"weatherFile":"')
    j = html.find('"', i)
    if j < 0 or j - i > 300:
        return None
    return html[i:j].replace("\\/", "/")


def fetch_mcp(resort):
    html = _get(MCP_PAGES[resort])
    updated_text = None
    url = _mcp_weather_json_url(html)
    if url:
        try:  # "Snowfall totals updated: ..." is filled client-side from this file
            d = requests.get(url, headers=BROWSER_HEADERS, timeout=30).json()
            updated_text = d.get("conditions_table_updated") or None
        except Exception as e:
            print(f"  [mcp] {resort}: weather.json unavailable ({type(e).__name__}); no updated time")
    return parse_mcp(resort, html, updated_text)


def fetch_purgatory():
    return fetch_mcp("Purgatory")


def fetch_sipapu():
    return fetch_mcp("Sipapu")


def fetch_pajarito():
    return fetch_mcp("Pajarito")


# ---------------------------------------------------------------- Wolf Creek

# "Latest Storm" is Wolf Creek's storm total (it keeps showing the last storm until the next one).
# Summit / Midway snow depth: two depths, neither labeled "base" -> raw only.
WOLF_CREEK_EXTRA = {"latest storm": "storm_total_in"}


_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def _wolf_updated(header, weather_time):
    """Header 'Mar 25, 2026 - 07:03' is a 12-hour clock with no AM/PM ('Sep 09, 2026 - 05:09'
    sits next to 'Current Weather as of (17:09)'). Take the date from the header and the
    24-hour time from the weather line when the two agree; otherwise give up (None)."""
    if not header or " - " not in header or len(header) > 40:
        return None
    date_part, _, hhmm = header.rpartition(" - ")
    m1, m2 = _HHMM.match(hhmm.strip()), _HHMM.match((weather_time or "").strip())
    if not (m1 and m2):
        return None
    h12, h24 = int(m1.group(1)), int(m2.group(1))
    if m1.group(2) != m2.group(2) or h12 % 12 != h24 % 12:
        return None
    return _strptime(f"{date_part.strip()} {h24:02d}:{m2.group(2)}", "%b %d, %Y %H:%M")


def parse_wolf_creek(html):
    # <div class="snow-report-text-div"><div class="snow-report-left-text">24 hours</div>
    #   <div class="snow-report-right-text"><span>12</span>"</div></div>
    # Header: <h4 class="snow-report-top-head">Updated: <span>Mar 25, 2026 - 07:03</span></h4>
    # Weather row: "Current Weather as of (<span>07:03</span>):" (same moment, 24-hour clock)
    updated_text = weather_time = None
    nodes = _nodes(html)
    for i, (text, anc) in enumerate(nodes):
        if updated_text is None and _has(anc, "snow-report-top-head") and anc[-1][0] == "span":
            updated_text = text
        if weather_time is None and text.startswith("Current Weather as of") and i + 1 < len(nodes):
            weather_time = nodes[i + 1][0]
    pairs = _grouped(html, "snow-report-text-div",
                     lambda inner: _has(inner, "snow-report-left-text"),
                     lambda inner: _has(inner, "snow-report-right-text"))
    snow = [(l, v) for l, v in pairs if v.strip().endswith('"') or v.strip().endswith("”")]
    values, source, unmapped = _classify("Wolf Creek", snow, WOLF_CREEK_EXTRA,
                                         required=("snow_24h_in", "season_total_in"))
    depths = {k: _value(v, f"Wolf Creek {k!r}") for k, v in unmapped.items()
              if _norm(k) in ("summit snow depth", "midway snow depth")}
    updated_at = _wolf_updated(updated_text, weather_time)
    return [report("Wolf Creek", "wolfcreek_html", updated_at, updated_text,
                   raw={"source": source, "unmapped": unmapped, "depths_in": depths,
                        "weather_as_of": weather_time}, **values)]


def fetch_wolf_creek():
    return parse_wolf_creek(_get(WOLF_CREEK_URL))


# ---------------------------------------------------------------- Telluride

TELLURIDE_EXTRA = {"snow depth": "base_depth_in"}  # the only depth Telluride publishes


def parse_telluride(html):
    # <div class="tsr-report-snow-depth"><p><span>Snow depth</span><br><span><strong>36"</strong></span></p>
    # <div class="tsr-report-app-snow-totals-total"><p><span>Snow overnight</span><span><strong>0"</strong>...
    pairs = []
    for cls in ("tsr-report-snow-depth", "tsr-report-app-snow-totals-total"):
        cur = None
        for text, anc in _nodes(html):
            ci = _index(anc, cls)
            if ci < 0:
                continue
            inner = anc[ci + 1:]
            if inner and inner[-1][0] == "strong":
                if cur is not None:
                    pairs.append((cur, text))
                    cur = None
            else:
                cur = text
    # "Last Updated: October 19, 2026 at 6:46pm" is the page render time (it equals the fetch
    # time on every Wayback capture), not when patrol updated the numbers -> raw only.
    rendered = None
    for text, _ in _nodes(html):
        if text.startswith("Last Updated:") and len(text) < 80:
            rendered = text[len("Last Updated:"):].strip()
            break
    values, source, unmapped = _classify("Telluride", pairs, TELLURIDE_EXTRA,
                                         required=("snow_24h_in",))
    return [report("Telluride", "telluride_html", None, None,
                   raw={"source": source, "unmapped": unmapped, "page_rendered": rendered},
                   **values)]


def fetch_telluride():
    return parse_telluride(_get(TELLURIDE_URL))


# ---------------------------------------------------------------- Taos

def parse_taos(html):
    # Next.js page, "WEATHER CONDITIONS / Powered By OpenSnow" card:
    #   <p>Snow Fall</p> ... <div><p>24 Hours</p><h4>0&quot;</h4></div><div><p>48 Hours</p>...
    #   ... <p>7 Day</p><h4>24"</h4> ... <p>Base Depth</p><h4>0"</h4> ... <p>Wind Speed</p>
    nodes = _nodes(html)
    start = next((i for i, (t, _) in enumerate(nodes) if t == "Snow Fall"), None)
    if start is None:
        raise RuntimeError("Taos: 'Snow Fall' card not on the page")
    pairs, label = [], None
    for text, anc in nodes[start + 1:start + 40]:
        tag = anc[-1][0] if anc else ""
        if tag == "p":
            if _norm(text).startswith("wind"):
                break
            label = text
        elif tag == "h4" and label is not None:
            pairs.append((label, text))
            label = None
    values, source, unmapped = _classify("Taos", pairs, required=("snow_24h_in",))
    # No timestamp on the card. Numbers come through OpenSnow's resort API.
    return [report("Taos", "taos_html", None, None,
                   raw={"source": source, "unmapped": unmapped, "provider": "OpenSnow"}, **values)]


def fetch_taos():
    return parse_taos(_get(TAOS_URL))


# ---------------------------------------------------------------- Ski Santa Fe

def _next_f_text(html):
    """Concatenated strings of every self.__next_f.push([1,"..."]) chunk (Next.js RSC payload)."""
    out, i, dec = [], 0, json.JSONDecoder()
    marker = "self.__next_f.push("
    while True:
        i = html.find(marker, i)
        if i < 0:
            break
        i += len(marker)
        try:
            arr, i = dec.raw_decode(html, i)
        except ValueError:
            continue
        if isinstance(arr, list) and len(arr) == 2 and isinstance(arr[1], str):
            out.append(arr[1])
    return "".join(out)


SANTA_FE_KEYS = {"snow24h": "snow_24h_in", "snow48h": "snow_48h_in", "snow72h": "snow_72h_in",
                 "snow7d": "snow_7d_in", "stormTotal": "storm_total_in",
                 "seasonTotal": "season_total_in", "baseDepth": "base_depth_in"}


def parse_santa_fe(html):
    # RSC payload: "conditionsData":{..."weather":{..."snowReport":{"snow12h":0,"snow24h":0,
    #   "snow48h":0,"snow72h":0,"snow5d":0,"snow7d":0,"stormTotal":0,"seasonTotal":75,
    #   "baseDepth":0,"summitDepth":0},...},..."snowReportLastUpdated":"2026-10-19T01:00:03.375Z"}
    t = _next_f_text(html)
    i = t.find('"snowReport":{')
    if i < 0:
        raise RuntimeError("Ski Santa Fe: snowReport object not in the page payload")
    sr, _ = json.JSONDecoder().raw_decode(t, i + len('"snowReport":'))
    j = t.find('"snowReportLastUpdated":"')
    updated_text = None
    if j >= 0:
        k = t.find('"', j + len('"snowReportLastUpdated":"'))
        updated_text = t[j + len('"snowReportLastUpdated":"'):k]
        if len(updated_text) > 40:
            updated_text = None
    values = {f: num(sr.get(k)) for k, f in SANTA_FE_KEYS.items()}
    source = {f: f"snowReport.{k} -> {sr.get(k)!r}" for k, f in SANTA_FE_KEYS.items()}
    return [report("Ski Santa Fe", "skisantafe_json", _iso(updated_text), updated_text,
                   raw={"source": source, "snowReport": sr}, **values)]


def fetch_santa_fe():
    return parse_santa_fe(_get(SANTA_FE_URL))


# ---------------------------------------------------------------- Angel Fire

_ANGEL_TIME = re.compile(r"^updated [a-z]{3,9}\.? \d{1,2} at (\d{1,2}:\d{2} ?[ap]m)$")


def parse_angel_fire(html):
    # E-mail style page: <span>Updated Mar 22 at 5:38 am</span> ... <h2>March 22, 2026</h2>
    # <td class="report section"><h4>Snow Totals</h4> ... <span>Base Depth</span> <span>16"</span>
    #   <span>Season Total</span> <span>63"</span>  (label, value spans alternate)
    nodes = _nodes(html)
    time_text = date_text = None
    pairs, label, in_snow = [], None, False
    for text, anc in nodes:
        tag = anc[-1][0] if anc else ""
        if time_text is None and text.lower().startswith("updated ") and len(text) < 60:
            time_text = text
        elif date_text is None and tag == "h2" and _has(anc, "section", 2):
            date_text = text
        if tag in ("h1", "h2", "h3", "h4"):
            in_snow = _norm(text) == "snow totals"
            continue
        if in_snow and tag == "span":
            if label is None:
                label = text
            else:
                pairs.append((label, text))
                label = None
    if not pairs:
        raise RuntimeError("Angel Fire: 'Snow Totals' block not found")
    values, source, unmapped = _classify("Angel Fire", pairs)
    updated_text = updated_at = None
    if time_text and date_text:
        m = _ANGEL_TIME.match(time_text.strip().lower())
        updated_text = f"{time_text} ({date_text})"
        if m:
            updated_at = _strptime(f"{date_text} {m.group(1).replace(' ', '')}", "%B %d, %Y %I:%M%p")
    return [report("Angel Fire", "angelfire_html", updated_at, updated_text,
                   raw={"source": source, "unmapped": unmapped}, **values)]


def fetch_angel_fire():
    return parse_angel_fire(_get(ANGEL_FIRE_URL))


# ---------------------------------------------------------------- Red River

def parse_red_river(html):
    # <dl class="date-info"><dt>Updated</dt><dd>07:10 AM, March 23, 2026</dd></dl>
    # <div class="SnowReport-measures ..."><dl class="SnowReport-measure"><dt>Base Depth</dt><dd>0"</dd></dl>...
    updated_text, pairs, label = None, [], None
    for text, anc in _nodes(html):
        tag = anc[-1][0] if anc else ""
        if updated_text is None and tag == "dd" and _has(anc, "date-info"):
            updated_text = text
        if _has(anc, "SnowReport-measure", 2) and not _has(anc, "trails-lifts"):
            if tag == "dt":
                label = text
            elif tag == "dd" and label is not None:
                pairs.append((label, text))
                label = None
    values, source, unmapped = _classify("Red River", pairs,
                                         required=("snow_24h_in", "base_depth_in", "season_total_in"))
    updated_at = _strptime(updated_text or "", "%I:%M %p, %B %d, %Y")
    return [report("Red River", "redriver_html", updated_at, updated_text,
                   raw={"source": source, "unmapped": unmapped}, **values)]


def fetch_red_river():
    return parse_red_river(_get(RED_RIVER_URL))


# ---------------------------------------------------------------- Ski Apache

def parse_ski_apache(html):
    # Conditions panel (rendered twice): <span>Last Updated: September 6, 2026 2:06pm</span>
    #   <div class="cell small-6"><p>Past 24 Hours: 0"</p></div> ... <p>Season Total:</p>
    updated_text, pairs, seen = None, [], set()
    for text, anc in _nodes(html):
        if not _has(anc, "grid-x"):
            continue
        if updated_text is None and text.startswith("Last Updated:") and len(text) < 80:
            updated_text = text[len("Last Updated:"):].strip()
            continue
        if anc[-1][0] == "p" and _has(anc, "small-6", 2) and ":" in text and len(text) < 80:
            label, _, value = text.partition(":")
            if label in seen:
                break  # second copy of the panel
            seen.add(label)
            pairs.append((label.strip(), value.strip()))
    # The same grid also lists lifts / trails / summer activities ("ZipTour:", "Gondola:").
    snow = [(l, v) for l, v in pairs if _field(l) or v.translate(_QUOTES).strip().endswith('"')]
    values, source, unmapped = _classify("Ski Apache", snow, required=("snow_24h_in",))
    # "September 6, 2026 2:06pm" / "December 5, 2025 7:24 am"
    updated_at = _strptime(updated_text or "", "%B %d, %Y %I:%M%p", "%B %d, %Y %I:%M %p")
    return [report("Ski Apache", "skiapache_html", updated_at, updated_text,
                   raw={"source": source, "unmapped": unmapped}, **values)]


def fetch_ski_apache():
    return parse_ski_apache(_get(SKI_APACHE_URL))


# ---------------------------------------------------------------- Silverton

def parse_silverton(html):
    # Header widget on every page: <div class="weather"><div class="number"> 0&rdquo;</div>
    #   <div class="description">24 hours</div></div> ... 48 hours. Hand-entered ("o”" seen
    # for 0). No timestamp, no base / season / storm numbers anywhere on the site.
    pairs = _grouped(html, "weather",
                     lambda inner: _has(inner, "description"),
                     lambda inner: _has(inner, "number"))
    seen, uniq = set(), []
    for l, v in pairs:
        if l not in seen:
            seen.add(l)
            uniq.append((l, v))
    values, source, unmapped = _classify("Silverton", uniq, required=("snow_24h_in",))
    return [report("Silverton", "silverton_html", None, None,
                   raw={"source": source, "unmapped": unmapped}, **values)]


def fetch_silverton():
    return parse_silverton(_get(SILVERTON_URL))


# ---------------------------------------------------------------- Ski Cooper

def parse_ski_cooper(html):
    # The report is a JPEG: <div class="snow-report-images"><div class="report-image">
    #   <h3>Snow Report</h3><img src=".../SNOW_CONDITIONS_2026-04-07.jpg"></div>
    i = html.find('class="snow-report-images"')
    img = None
    if i >= 0:
        j = html.find("<img", i)
        k = html.find('src="', j) if j >= 0 else -1
        if k >= 0:
            img = html[k + 5:html.find('"', k + 5)][:300]
    raise RuntimeError("Ski Cooper publishes its snow report only as an image "
                       f"({img or 'no image found'}); no machine-readable numbers")


def fetch_ski_cooper():
    return parse_ski_cooper(_get(SKI_COOPER_URL))


SOUTH_SOURCES = [
    ("purgatory_html", fetch_purgatory),
    ("wolfcreek_html", fetch_wolf_creek),
    ("telluride_html", fetch_telluride),
    ("taos_html", fetch_taos),
    # ("skicooper_html", fetch_ski_cooper),  # Ski Cooper posts its report as an image only
    ("skisantafe_json", fetch_santa_fe),
    ("angelfire_html", fetch_angel_fire),
    ("redriver_html", fetch_red_river),
    ("sipapu_html", fetch_sipapu),
    ("skiapache_html", fetch_ski_apache),
    ("pajarito_html", fetch_pajarito),
    ("silverton_html", fetch_silverton),
]

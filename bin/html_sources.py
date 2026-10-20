"""HTML snow-report sources for small resorts without a JSON feed.

Standard library + requests only. Pages are parsed with html.parser into text nodes tagged
with their ancestors' CSS classes; the only regexes run on short extracted strings (< 200
chars), never on raw page HTML (these pages carry very long minified lines).

Each fetch_* returns [report(...)] and raises if the expected labels aren't on the page.
Each parse_* takes the page HTML so the mapping can be tested against saved snapshots.
raw["source"] records the exact label -> value text every field came from.
"""
import re
from datetime import datetime
from html.parser import HTMLParser

import requests

from fetch_resort_reports import report, num, UA, BROWSER_HEADERS, LOCAL_TZ  # noqa: F401

MONARCH_URL = "https://skimonarch.com/conditions/"
# powderhorn.com/conditions-report.php (the old hdrelay widget) 404s since the 2026 site
# rebuild; the snow numbers now live on /conditions/.
POWDERHORN_URL = "https://www.powderhorn.com/conditions/"
DISCOVERY_URL = "https://www.skidiscovery.com/snow-report/"
BRIAN_HEAD_URL = "https://www.brianhead.com/weather-conditions-webcams/"
# The page's own JS fills "Snowfall totals updated: ..." from this file.
BRIAN_HEAD_JSON = "https://www.brianhead.com/wp-content/uploads/sites/7/m-json/weather.json"
WHITEFISH_URL = "https://skiwhitefish.com/snowreport/"
SUNLIGHT_URL = "https://sunlightmtn.com/explore/mountain-status/"

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "source", "track", "wbr"}
_SKIP = {"script", "style", "noscript", "svg", "template"}


class _Nodes(HTMLParser):
    """Collect (text, ancestors) for every visible text node; ancestors = [(tag, {classes})]."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.nodes, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        if tag in _SKIP:
            self.skip += 1
        cls = set((dict(attrs).get("class") or "").split())
        self.stack.append((tag, cls))

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                if tag in _SKIP:
                    self.skip = max(0, self.skip - 1)
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self.skip:
            return
        t = " ".join(data.split())
        if t:
            self.nodes.append((t, list(self.stack)))


def _nodes(html):
    p = _Nodes()
    p.feed(html)
    p.close()
    return p.nodes


def _has(anc, cls, depth=None):
    """True if any ancestor (optionally only the nearest `depth`) carries CSS class `cls`."""
    seq = anc if depth is None else anc[-depth:]
    return any(cls in c for _, c in seq)


def _index(anc, cls):
    """Index into the ancestor list of the innermost element with class `cls` (or -1)."""
    for i in range(len(anc) - 1, -1, -1):
        if cls in anc[i][1]:
            return i
    return -1


_DASHES = {"", "-", "--", "–", "—", "n/a", "na"}
_QUOTES = str.maketrans({"”": '"', "″": '"', "“": '"', "’": "'"})
_INCHES = re.compile(r'^(\d+(?:\.\d+)?)\s*(?:"|in\.?|inch(?:es)?)?$')


def _inches(text, where):
    """'12"' / '12”' / '12 in' -> 12.0; dash/blank -> None; anything else raises."""
    s = (text or "").translate(_QUOTES).strip().lower()
    if s in _DASHES:
        return None
    if s in ("t", "tr", "trace", "t\"", "trace\""):  # trace amounts are posted all winter
        return 0.0
    if len(s) > 40:
        raise ValueError(f"{where}: value too long to be a snow number: {s[:60]!r}")
    m = _INCHES.match(s)
    if not m:
        raise ValueError(f"{where}: unexpected snow value {text!r}")
    return num(m.group(1))


def _norm(label):
    return " ".join(label.translate(_QUOTES).lower().replace(":", " ").split())


def _map(resort, pairs, labels, required):
    """pairs: [(label_text, value_text)]; labels: {normalized label: field}.

    Returns (values, source). The first occurrence of a field wins. Raises if none of the
    `required` fields' labels were found (page layout changed or report hidden)."""
    values, source = {}, {}
    for label, value in pairs:
        field = labels.get(_norm(label))
        if not field or field in source:
            continue
        values[field] = _inches(value, f"{resort} {label!r}")
        source[field] = f"{label} -> {value!r}"
    missing = [f for f in required if f not in source]
    if missing:
        raise RuntimeError(f"{resort}: labels for {missing} not found on page "
                           f"(saw {[l for l, _ in pairs][:20]})")
    return values, source


def _strptime(text, *fmts):
    for fmt in fmts:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            pass
    return None


def _get(url, **kw):
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=45, **kw)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------- Monarch

MONARCH_LABELS = {"overnight": "snow_overnight_in", "24 hours": "snow_24h_in",
                  "48 hours": "snow_48h_in", "7 days": "snow_7d_in",
                  "base": "base_depth_in", "season total": "season_total_in"}


def parse_monarch(html):
    # <table class="accumulation__table"><thead><th>0"</th>...</thead>
    #   <tbody><tr><td>Overnight</td>...</tr></tbody></table>  -- th[i] is the value for td[i]
    # The whole table is omitted from the page off-season.
    heads, cells, updated_text = [], [], None
    for text, anc in _nodes(html):
        if _has(anc, "accumulation__table"):
            tag = anc[-1][0]
            if tag == "th":
                heads.append(text)
            elif tag == "td":
                cells.append(text)
        elif _has(anc, "update-info", 1) and text.startswith("Last Updated:"):
            updated_text = text[len("Last Updated:"):].strip()
    if not cells:
        raise RuntimeError("Monarch: snow table (table.accumulation__table) not on the page "
                           "- Monarch removes it off-season")
    if len(heads) != len(cells):
        raise RuntimeError(f"Monarch: {len(heads)} values vs {len(cells)} labels: {heads} / {cells}")
    values, source = _map("Monarch", list(zip(cells, heads)), MONARCH_LABELS,
                          required=("snow_24h_in", "base_depth_in", "season_total_in"))
    # "February 11, 2026 - 05:47 AM"
    updated_at = _strptime(updated_text or "", "%B %d, %Y - %I:%M %p")
    return [report("Monarch", "monarch_html", updated_at, updated_text,
                   raw={"source": source, "labels": dict(zip(cells, heads))}, **values)]


def fetch_monarch():
    return parse_monarch(_get(MONARCH_URL))


# ---------------------------------------------------------------- Powderhorn

POWDERHORN_LABELS = {"24 hours": "snow_24h_in", "48 hours": "snow_48h_in",
                     "7 days": "snow_7d_in", "base depth": "base_depth_in",
                     "season total": "season_total_in"}


def parse_powderhorn(html):
    # "Fresh & base / Snowfall" block: <div class="pgri-stats"> ... columns of
    #   <p class="pgri-stat-n">—</p><p class="pgri-stat-l">24 Hours</p>   (value BEFORE label)
    # The "Current Conditions" card above it (pgri-forecast__stat: "24hr snow", "Base–Peak
    # range") is OpenSnow's weather model, not the resort's report -> raw only.
    stats, model = [], []
    pending = None
    for text, anc in _nodes(html):
        if _has(anc, "pgri-stats"):
            if _has(anc, "pgri-stat-n", 1):
                pending = text
            elif _has(anc, "pgri-stat-l", 1):
                stats.append((text, pending if pending is not None else ""))
                pending = None
        elif _has(anc, "pgri-forecast__stat"):
            model.append(text)
    values, source = _map("Powderhorn", stats, POWDERHORN_LABELS,
                          required=("snow_24h_in", "base_depth_in", "season_total_in"))
    opensnow = dict(zip(model[1::2], model[0::2]))  # value, label, value, label ...
    # No "updated" text is published for this block.
    return [report("Powderhorn", "powderhorn_html", None, None,
                   raw={"source": source, "labels": {l: v for l, v in stats},
                        "opensnow_model": opensnow}, **values)]


def fetch_powderhorn():
    return parse_powderhorn(_get(POWDERHORN_URL))


# ---------------------------------------------------------------- Discovery

DISCOVERY_LABELS = {"new snow (since lifts closed)": "snow_overnight_in",
                    "new snow, 24 hours": "snow_24h_in",
                    "new snow, 48 hours": "snow_48h_in",
                    "new snow, 72 hours": "snow_72h_in",
                    "snowfall, ytd": "season_total_in",
                    "snow depth - top": "base_depth_in"}


def parse_discovery(html):
    # <span class="tablinks">Updated: April 7, 2026 at 10:07 am</span>
    # <div class="main-content"><div class="row"><p>New Snow, 24 hours</p>
    #                                           <p class="little-text inch">0</p></div>...
    # Values are left blank off-season.
    rows, updated_text = [], None
    cur_row, cur = None, []
    for text, anc in _nodes(html):
        if _has(anc, "tablinks", 1) and text.startswith("Updated:") and updated_text is None:
            updated_text = text[len("Updated:"):].strip()
            continue
        if not _has(anc, "main-content"):
            continue
        ri = _index(anc, "row")
        if ri < 0:
            continue
        key = id(anc[ri][1])
        if key != cur_row:
            if cur:
                rows.append(cur)
            cur_row, cur = key, []
        # label <p> has no class; value <p class="little-text ..."> (may contain a nested span)
        cur.append((text, _has(anc, "little-text")))
    if cur:
        rows.append(cur)
    pairs = []
    for r in rows:
        label = " ".join(t for t, is_val in r if not is_val)
        value = "".join(t for t, is_val in r if is_val)
        if label:
            pairs.append((label, value))
    wanted = [(l, v) for l, v in pairs if _norm(l) in DISCOVERY_LABELS or _norm(l) == "snow depth - bottom"]
    values, source = _map("Discovery", wanted, DISCOVERY_LABELS,
                          required=("snow_24h_in", "base_depth_in", "season_total_in"))
    bottom = next((v for l, v in wanted if _norm(l) == "snow depth - bottom"), None)
    raw = {"source": source, "labels": dict(wanted),
           "depth_bottom_in": _inches(bottom, "Discovery bottom depth") if bottom is not None else None}
    # "April 7, 2026 at 10:07 am"
    updated_at = _strptime(updated_text or "", "%B %d, %Y at %I:%M %p")
    return [report("Discovery", "discovery_html", updated_at, updated_text, raw=raw, **values)]


def fetch_discovery():
    return parse_discovery(_get(DISCOVERY_URL))


# ---------------------------------------------------------------- Brian Head

BRIAN_HEAD_LABELS = {"24 hr": "snow_24h_in", "24hr": "snow_24h_in",
                     "48 hr": "snow_48h_in", "48hr": "snow_48h_in",
                     "7 day": "snow_7d_in",
                     "base depth": "base_depth_in", "base": "base_depth_in",
                     "season total": "season_total_in", "season": "season_total_in"}


def parse_brian_head(html, updated_text=None):
    # <div class="m-snow-totals-table ..."><div>
    #    <div class="m-snow-totals-top">0”</div><div class="m-snow-totals-label">24 Hr</div>
    # </div>...   (the same table is rendered twice on the page; the first one is used)
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
    known = [(l, v) for l, v in pairs if _norm(l) in BRIAN_HEAD_LABELS]
    values, source = _map("Brian Head", known, BRIAN_HEAD_LABELS,
                          required=("snow_24h_in", "base_depth_in", "season_total_in"))
    # weather.json conditions_table_updated: "May 4, 2026 9:01 am"
    updated_at = _strptime(updated_text or "", "%B %d, %Y %I:%M %p")
    return [report("Brian Head", "brianhead_html", updated_at, updated_text,
                   raw={"source": source, "labels": dict(pairs)}, **values)]


def fetch_brian_head():
    html = _get(BRIAN_HEAD_URL)
    updated_text = None
    try:  # the "Snowfall totals updated: ..." line is filled client-side from this JSON
        d = requests.get(BRIAN_HEAD_JSON, headers=BROWSER_HEADERS, timeout=30).json()
        updated_text = d.get("conditions_table_updated") or None
    except Exception as e:
        print(f"  [brianhead] weather.json unavailable ({type(e).__name__}); no updated time")
    return parse_brian_head(html, updated_text)


# ---------------------------------------------------------------- Whitefish

WHITEFISH_SUMMIT = {"overnight fresh": "snow_overnight_in", "past 24 hours": "snow_24h_in",
                    "past 7 days": "snow_7d_in", "settled base": "base_depth_in",
                    "season to date": "season_total_in"}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_STORY_DATE = re.compile(r"^(?:mon|tues|wednes|thurs|fri|satur|sun)day,\s+([a-z]{3,9})\.?\s+(\d{1,2}),\s+(\d{4}):?$")


def _whitefish_story_date(text):
    """'Wednesday, Jan. 14, 2026:' -> date (the daily report's dateline)."""
    s = text.strip().lower()
    if len(s) > 60:
        return None
    m = _STORY_DATE.match(s)
    if not m or m.group(1)[:3] not in _MONTHS:
        return None
    try:
        return datetime(int(m.group(3)), _MONTHS[m.group(1)[:3]], int(m.group(2)), 6, 0, tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def parse_whitefish(html):
    # <div class="wmr_snow_stat_group"><div class="wmr_snow_stat_group_header">Summit <em>6,817 feet</em></div>
    #   <div class="wmr_snow_stat_row"><span class="wmr_snow_stat_label">Past 24 Hours</span>
    #                                  <span class="wmr_snow_stat_value">0"</span></div> ...
    # Second group "Base Area" (24h / 7d, natural snowfall only) -> raw.
    groups, group, label = {}, None, None
    updated_at = updated_text = None
    for text, anc in _nodes(html):
        if _has(anc, "wmr_snow_stat_group_header"):
            if not _has(anc, "wmr_snow_elevation", 1):
                group = text
                groups.setdefault(group, [])
            continue
        if _has(anc, "wmr_snow_stat_group") and group is not None:
            if _has(anc, "wmr_snow_stat_label", 1):
                label = text
            elif _has(anc, "wmr_snow_stat_value", 1) and label is not None:
                groups[group].append((label, text))
                label = None
        elif updated_at is None and _has(anc, "wmr_snow_story_text"):
            dt = _whitefish_story_date(text)
            if dt:
                updated_at, updated_text = dt, text
    summit = groups.get("Summit")
    if summit is None:
        raise RuntimeError(f"Whitefish: 'Summit' stat group not found (groups: {list(groups)})")
    values, source = _map("Whitefish", summit, WHITEFISH_SUMMIT,
                          required=("snow_24h_in", "base_depth_in", "season_total_in"))
    base_area = {l: _inches(v, f"Whitefish base area {l!r}") for l, v in groups.get("Base Area", [])}
    return [report("Whitefish", "whitefish_html", updated_at, updated_text,
                   raw={"source": source, "summit": dict(summit), "base_area_in": base_area,
                        "base_area_labels": dict(groups.get("Base Area", []))}, **values)]


def fetch_whitefish():
    return parse_whitefish(_get(WHITEFISH_URL))


# ---------------------------------------------------------------- Sunlight

# Two panels show the same numbers: "At A Glance" (Since 4pm / 24 Hrs / 48 Hrs / Base Depth)
# and "Current Status" (Since 4PM / Last 24 Hours / Last 48 Hours / Season Total / Base Depth).
SUNLIGHT_LABELS = {"since 4pm": "snow_overnight_in",
                   "last 24 hours": "snow_24h_in", "24 hrs": "snow_24h_in",
                   "last 48 hours": "snow_48h_in", "48 hrs": "snow_48h_in",
                   "base depth": "base_depth_in", "season total": "season_total_in"}


def parse_sunlight(html):
    # Elementor icon boxes: <div class="elementor-icon-box-content">
    #   <span class="elementor-icon-box-title"><span>Since 4PM</span></span>
    #   <p class="elementor-icon-box-description">0&quot;</p></div>
    boxes, order = {}, []
    for text, anc in _nodes(html):
        ci = _index(anc, "elementor-icon-box-content")
        if ci < 0:
            continue
        key = id(anc[ci][1])
        if key not in boxes:
            boxes[key] = {"title": [], "desc": []}
            order.append(key)
        if _has(anc[ci:], "elementor-icon-box-title"):
            boxes[key]["title"].append(text)
        elif _has(anc[ci:], "elementor-icon-box-description"):
            boxes[key]["desc"].append(text)
    pairs = [(" ".join(boxes[k]["title"]), " ".join(boxes[k]["desc"])) for k in order]
    pairs = [(l, v) for l, v in pairs if _norm(l) in SUNLIGHT_LABELS]
    # "Current Status" panel comes second on the page; prefer it, keep both in raw.
    values, source = _map("Sunlight", list(reversed(pairs)), SUNLIGHT_LABELS,
                          required=("snow_24h_in", "base_depth_in"))
    if "season_total_in" not in source:
        raise RuntimeError("Sunlight: 'Season Total' label not found")
    # No per-report timestamp on the page (article:modified_time is the page edit time,
    # which does not move when the numbers change).
    return [report("Sunlight", "sunlight_html", None, None,
                   raw={"source": source, "labels": pairs}, **values)]


def fetch_sunlight():
    return parse_sunlight(_get(SUNLIGHT_URL))


HTML_SOURCES = [
    ("monarch_html", fetch_monarch),
    ("powderhorn_html", fetch_powderhorn),
    ("discovery_html", fetch_discovery),
    ("brianhead_html", fetch_brian_head),
    ("whitefish_html", fetch_whitefish),
    ("sunlight_html", fetch_sunlight),
]

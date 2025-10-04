import os
import sys
import time
from datetime import datetime, timezone, date, timedelta
from dateutil import parser as dtparse
import re
import requests
import psycopg
from collections import Counter
import xml.etree.ElementTree as ET

DB_URL = os.getenv("DATABASE_URL", "postgresql://snowuser:changeme@db:5432/snow")

AWDB_BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"

FEET_TO_M = 0.3048
IN_TO_CM = 2.54

REQUEST_TIMEOUT = int(os.getenv("AWDB_TIMEOUT_SEC", "45"))
AWDB_RETRIES    = int(os.getenv("AWDB_RETRIES", "3"))
AWDB_BACKOFF    = float(os.getenv("AWDB_BACKOFF", "0.75"))

CHUNK_SIZE      = int(os.getenv("AWDB_CHUNK_SIZE", "20"))  # was 50
FORECAST_DAYS_AHEAD = int(os.getenv("FORECAST_DAYS_AHEAD", "14"))


# Per-model Open-Meteo endpoints (snowfall supported on these)
OM_ENDPOINTS = {
    "gfs":   "https://api.open-meteo.com/v1/gfs",
    "icon":  "https://api.open-meteo.com/v1/dwd-icon",   # ← was /v1/icon (404)
    "ecmwf": "https://api.open-meteo.com/v1/ecmwf",
}
LAT = float(os.getenv("LAT", "39.2"))
LON = float(os.getenv("LON", "-106.9"))
TZ  = os.getenv("TIMEZONE", "America/Denver")
MODELS = [m.strip().lower() for m in os.getenv("MODELS", "gfs").split(",") if m.strip()]

VERBOSE = os.getenv("VERBOSE", "0") == "1"

def vlog(msg: str):
    if VERBOSE:
        print(msg, flush=True)

def pg():
    return psycopg.connect(DB_URL, autocommit=True)

# Triplet format helper
TRIPLET_RX = re.compile(r"^[A-Za-z0-9]+:[A-Z]{2}:SNTL$")  # e.g., 663:CO:SNTL

def upsert_snotel_metadata(states=None):
    headers = {"User-Agent": "SnowAPI/1.0 (+you@domain)"}
    params = {"networkCds": "SNTL"}
    if states:
        params["stateCds"] = ",".join(states)

    vlog(f"[snotel] using AWDB_BASE={AWDB_BASE} with params={params}")
    r = requests.get(f"{AWDB_BASE}/stations", params=params, headers=headers, timeout=120)
    vlog(f"[snotel] stations GET {r.url} status={r.status_code}")
    r.raise_for_status()

    data = r.json()
    if isinstance(data, dict):
        src_list = [data]
    elif isinstance(data, list):
        src_list = data
    else:
        src_list = []

    vlog(f"[snotel] stations payload type={type(data).__name__} len={len(src_list)}")
    if src_list[:3]:
        vlog(f"[snotel] first items sample: {src_list[:3]}")

    if states:
        state_set = {s.upper() for s in states}
        kept = [s for s in src_list if s.get("networkCode") == "SNTL" and s.get("stateCode", "").upper() in state_set]
    else:
        kept = [s for s in src_list if s.get("networkCode") == "SNTL"]

    inserted = 0
    with pg() as conn, conn.cursor() as cur:
        for s in kept:
            try:
                trip = s.get("stationTriplet")
                name = (s.get("name") or "").strip()
                lat = float(s["latitude"]) if s.get("latitude") is not None else None
                lon = float(s["longitude"]) if s.get("longitude") is not None else None
                elev_ft = s.get("elevation")
                elev_m = float(elev_ft) * FEET_TO_M if elev_ft is not None else None

                if not trip or lat is None or lon is None:
                    vlog(f"[snotel] skipping record missing required fields: triplet={trip} lat={lat} lon={lon}")
                    continue

                cur.execute(
                    """
                    INSERT INTO snotel_station (station_triplet, name, latitude, longitude, elevation_m)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (station_triplet) DO UPDATE SET
                      name=EXCLUDED.name,
                      latitude=EXCLUDED.latitude,
                      longitude=EXCLUDED.longitude,
                      elevation_m=EXCLUDED.elevation_m
                    """,
                    (trip, name, lat, lon, elev_m)
                )
                inserted += 1
            except Exception as ex:
                print(f"[snotel] insert error {s.get('stationTriplet')}: {ex}")

    print(f"[snotel] upserted {inserted} stations (from {len(src_list)} records; kept {len(kept)})")


def _strip_ns(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag


def _awdb_probe_soap(triplet: str, days_back: int = 3):
    """Debugging helper: call SOAP for a single station and print raw status and a short body snippet."""
    b = date.today() - timedelta(days=days_back)
    e = date.today()
    try:
        rows, raw_rows = _awdb_get_daily_via_soap([triplet], b, e)
        print(f"[probe] rows={len(rows)} for {triplet} {b}..{e}")
        if rows[:5]:
            print("[probe] sample:", rows[:5])
        else:
            print("[probe] no rows parsed; enable VERBOSE=1 to see SOAP bodies/snippets")
        if raw_rows[:5]:
            print("[probe] raw sample (inches):", raw_rows[:5])
    except Exception as ex:
        print(f"[probe] ERROR: {ex}")


def _awdb_get_daily_via_soap(triplets: list[str], begin: date, end: date) -> tuple[list[dict], list[dict]]:
    """
    Call the SOAP service getData() to fetch DAILY SNWD/WTEQ/PREC (English units).
    Returns (rows_cm, rows_raw_in): rows_cm is list of {station_triplet, date, snow_depth_cm, swe_cm, precip_cm}; rows_raw_in is list of {station_triplet, date, snwd_in, wteq_in, prec_in}.
    """
    if not triplets:
        return []

    # SOAP endpoint (not the REST one)
    SOAP_URL = "https://wcc.sc.egov.usda.gov/awdbWebService/services"
    last_response_snippet = ""

    # Build the SOAP envelope. elementCd can be repeated entries.
    elts = "".join(f"<elementCd>{e}</elementCd>" for e in ("SNWD", "WTEQ", "PREC"))
    soap_bodies = [
        # SOAP 1.1, no-action header
        (
            "v11-noaction",
            f"""\
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soapenv:Header/>
  <soapenv:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <ordinal xsi:nil="true"/>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soapenv:Body>
</soapenv:Envelope>
""".encode("utf-8"),
            {
                "Content-Type": "text/xml; charset=utf-8",
                "User-Agent": "SnowAPI/1.0 (+you@domain)",
            }
        ),
        # SOAP 1.1, empty-action header
        (
            "v11-emptyaction",
            f"""\
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soapenv:Header/>
  <soapenv:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <ordinal xsi:nil="true"/>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soapenv:Body>
</soapenv:Envelope>
""".encode("utf-8"),
            {
                "Content-Type": "text/xml; charset=utf-8",
                "User-Agent": "SnowAPI/1.0 (+you@domain)",
                "SOAPAction": "",
            }
        ),
        # SOAP 1.1, with-action header
        (
            "v11-withaction",
            f"""\
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soapenv:Header/>
  <soapenv:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <ordinal xsi:nil="true"/>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soapenv:Body>
</soapenv:Envelope>
""".encode("utf-8"),
            {
                "Content-Type": "text/xml; charset=utf-8",
                "User-Agent": "SnowAPI/1.0 (+you@domain)",
                "SOAPAction": "\"http://www.wcc.nrcs.usda.gov/ns/awdbWebService/getData\"",
            }
        ),
        # SOAP 1.2, with-action
        (
            "v12-withaction",
            f"""\
<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soap12:Header/>
  <soap12:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <ordinal xsi:nil="true"/>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soap12:Body>
</soap12:Envelope>
""".encode("utf-8"),
            {
                "Content-Type": 'application/soap+xml; charset=utf-8; action="http://www.wcc.nrcs.usda.gov/ns/awdbWebService/getData"',
                "User-Agent": "SnowAPI/1.0 (+you@domain)",
            }
        ),
        # SOAP 1.2, no-action
        (
            "v12-noaction",
            f"""\
<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soap12:Header/>
  <soap12:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <ordinal xsi:nil="true"/>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soap12:Body>
</soap12:Envelope>
""".encode("utf-8"),
            {
                "Content-Type": 'application/soap+xml; charset=utf-8',
                "User-Agent": "SnowAPI/1.0 (+you@domain)",
            }
        ),
    ]

    last_err = None
    for attempt in range(1, AWDB_RETRIES + 1):
        for variant_name, body, headers in soap_bodies:
            try:
                r = requests.post(SOAP_URL, data=body, headers=headers, timeout=REQUEST_TIMEOUT)
                try:
                    last_response_snippet = r.text[:600].replace("\n", " ")
                except Exception:
                    last_response_snippet = str(r.content[:600])
                vlog(f"[awdb-soap] POST {SOAP_URL} ({variant_name}) attempt={attempt} status={r.status_code}")
                if r.status_code != 200:
                    try:
                        snippet = r.text[:600].replace("\n", " ")
                    except Exception:
                        snippet = str(r.content[:600])
                    vlog(f"[awdb-soap] ({variant_name}) non-200 response body (first 600 chars): {snippet}")
                try:
                    xml = ET.fromstring(r.content)
                except ET.ParseError as pe:
                    last_err = pe
                    try:
                        snippet = r.text[:600].replace("\n", " ")
                    except Exception:
                        snippet = str(r.content[:600])
                    vlog(f"[awdb-soap] ({variant_name}) non-XML response (status={r.status_code}): {snippet}")
                    continue  # Try next variant
                # Check for SOAP Fault
                fault = None
                for node in xml.iter():
                    if _strip_ns(node.tag).lower().endswith("faultstring") or _strip_ns(node.tag).lower() == "fault":
                        fault = (node.text or "").strip()
                        break
                if fault:
                    vlog(f"[awdb-soap] ({variant_name}) SOAP Fault: {fault}")
                    try:
                        snippet = r.text[:600].replace("\n", " ")
                    except Exception:
                        snippet = str(r.content[:600])
                    vlog(f"[awdb-soap] ({variant_name}) SOAP Fault body (first 600 chars): {snippet}")
                    return []
                # otherwise parsed OK
                break
            except Exception as e:
                last_err = e
                continue
        else:
            # All variants failed, backoff and retry
            time.sleep(AWDB_BACKOFF * attempt)
            continue
        # If we break (parsed OK), exit retry loop
        break
    else:
        # exhausted retries
        if last_err:
            raise last_err
        return []

    # Parse SOAP response
    # The structure is typically Envelope->Body->getDataResponse->return (repeated)
    # each return has stationTriplet, elementCd, values/value/date, values/value/value
    # Namespaces can vary, so compare by localname.
    ns_agnostic = lambda t: _strip_ns(t)

    # Determine whether any <return> blocks actually contain <values><value>
    returns = [n for n in xml.iter() if _strip_ns(n.tag) == "return"]
    has_values = False
    has_flat_values_text = False
    for ret in returns:
        for child in list(ret):
            if _strip_ns(child.tag) == "values":
                # Case A: nested <value> nodes
                for vv in list(child):
                    if _strip_ns(vv.tag) == "value":
                        has_values = True
                        break
                # Case B: compact text under <values>
                if child.text and child.text.strip():
                    has_flat_values_text = True
            if has_values:
                break
        if has_values:
            break

    # If response parsed but appears to have no <return> items OR none contain date/value pairs,
    # retry once using ordinal=1 (some deployments require ordinal instead of duration)
    if not returns or not has_values:
        if VERBOSE and returns:
            try:
                _first = returns[0]
                _dump = ET.tostring(_first, encoding="unicode")
                vlog(f"[awdb-soap] first <return> subtree (truncated): {(_dump[:800].replace(chr(10), ' '))}")
            except Exception:
                pass

        vlog("[awdb-soap] empty or valueless getData response; retrying once with ordinal=1 and no <duration>...")
        elts = "".join(f"<elementCd>{e}</elementCd>" for e in ("SNWD", "WTEQ", "PREC"))
        soap_bodies_ord1 = [
            # SOAP 1.1, no-action header
            (
                "v11-noaction-ord1",
                f"""\
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService">
  <soapenv:Header/>
  <soapenv:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <heightDepth>-1</heightDepth>
      <ordinal>1</ordinal>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soapenv:Body>
</soapenv:Envelope>
""".encode("utf-8"),
                {
                    "Content-Type": "text/xml; charset=utf-8",
                    "User-Agent": "SnowAPI/1.0 (+you@domain)",
                }
            ),
            # SOAP 1.1, empty-action header
            (
                "v11-emptyaction-ord1",
                f"""\
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService">
  <soapenv:Header/>
  <soapenv:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <heightDepth>-1</heightDepth>
      <ordinal>1</ordinal>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soapenv:Body>
</soapenv:Envelope>
""".encode("utf-8"),
                {
                    "Content-Type": "text/xml; charset=utf-8",
                    "User-Agent": "SnowAPI/1.0 (+you@domain)",
                    "SOAPAction": "",
                }
            ),
            # SOAP 1.1, with-action header
            (
                "v11-withaction-ord1",
                f"""\
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService">
  <soapenv:Header/>
  <soapenv:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <heightDepth>-1</heightDepth>
      <ordinal>1</ordinal>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soapenv:Body>
</soapenv:Envelope>
""".encode("utf-8"),
                {
                    "Content-Type": "text/xml; charset=utf-8",
                    "User-Agent": "SnowAPI/1.0 (+you@domain)",
                    "SOAPAction": "\"http://www.wcc.nrcs.usda.gov/ns/awdbWebService/getData\"",
                }
            ),
            # SOAP 1.2, with-action
            (
                "v12-withaction-ord1",
                f"""\
<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService">
  <soap12:Header/>
  <soap12:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <heightDepth>-1</heightDepth>
      <ordinal>1</ordinal>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soap12:Body>
</soap12:Envelope>
""".encode("utf-8"),
                {
                    "Content-Type": 'application/soap+xml; charset=utf-8; action="http://www.wcc.nrcs.usda.gov/ns/awdbWebService/getData"',
                    "User-Agent": "SnowAPI/1.0 (+you@domain)",
                }
            ),
            # SOAP 1.2, no-action
            (
                "v12-noaction-ord1",
                f"""\
<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService">
  <soap12:Header/>
  <soap12:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      {elts}
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <heightDepth>-1</heightDepth>
      <ordinal>1</ordinal>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soap12:Body>
</soap12:Envelope>
""".encode("utf-8"),
                {
                    "Content-Type": 'application/soap+xml; charset=utf-8',
                    "User-Agent": "SnowAPI/1.0 (+you@domain)",
                }
            ),
        ]
        # Try each variant in sequence
        for variant_name, body, headers in soap_bodies_ord1:
            try:
                r = requests.post(SOAP_URL, data=body, headers=headers, timeout=REQUEST_TIMEOUT)
                try:
                    last_response_snippet = r.text[:600].replace("\n", " ")
                except Exception:
                    last_response_snippet = str(r.content[:600])
                vlog(f"[awdb-soap] POST {SOAP_URL} ({variant_name}) (fallback ordinal=1) status={r.status_code}")
                if r.status_code != 200:
                    try:
                        snippet = r.text[:600].replace("\n", " ")
                    except Exception:
                        snippet = str(r.content[:600])
                    vlog(f"[awdb-soap] ({variant_name}) (ordinal=1) non-200 response body (first 600 chars): {snippet}")
                try:
                    xml = ET.fromstring(r.content)
                    # re-scan returns after ordinal retry
                    returns = [n for n in xml.iter() if _strip_ns(n.tag) == "return"]
                    has_values = False
                    has_flat_values_text = False
                    for ret in returns:
                        for child in list(ret):
                            if _strip_ns(child.tag) == "values":
                                for vv in list(child):
                                    if _strip_ns(vv.tag) == "value":
                                        has_values = True
                                        break
                                if child.text and child.text.strip():
                                    has_flat_values_text = True
                            if has_values:
                                break
                        if has_values:
                            break
                    break  # parsed XML successfully
                except ET.ParseError:
                    continue
            except Exception:
                continue

    # Collect values per (triplet, element) keyed by date
    buckets: dict[tuple[str, str], dict[date, float | None]] = {}
    stations_set, days_set = set(), set()

    # find all <return> entries
    for ret in (returns or []):
        if ns_agnostic(ret.tag) != "return":
            continue

        trip = None
        elem = None
        values_node = None
        for child in list(ret):
            tag = ns_agnostic(child.tag)
            if tag == "stationTriplet":
                trip = (child.text or "").strip()
            elif tag == "elementCd":
                elem = (child.text or "").strip()
            elif tag == "values":
                values_node = child

        if not trip or values_node is None:
            continue

        stations_set.add(trip)
        # If elementCd is missing in this format, we'll try to recover later via per-element fallback.
        key = (trip, elem if elem else "__UNKNOWN__")
        if key not in buckets:
            buckets[key] = {}

        # Case A: nested <value><date>…</date><value>…</value></value>
        nested_found = False
        for vv in list(values_node):
            if ns_agnostic(vv.tag) != "value":
                continue
            nested_found = True
            d_txt, v_txt = None, None
            for leaf in list(vv):
                ltag = ns_agnostic(leaf.tag)
                if ltag == "date":
                    d_txt = (leaf.text or "").strip()
                elif ltag == "value":
                    v_txt = (leaf.text or "").strip()
            if not d_txt:
                continue
            try:
                d = dtparse.isoparse(d_txt).date()
            except Exception:
                continue
            days_set.add(d)
            val = float(v_txt) if (v_txt not in (None, "", "NaN")) else None
            buckets[key][d] = val

        # Case B: compact text under <values> without dates — ignore here; handled by per-element fallback
        if (not nested_found) and (values_node.text and values_node.text.strip()):
            pass

    if VERBOSE and not stations_set:
        vlog("[awdb-soap] parsed OK but no <return> series present for this batch")
        if last_response_snippet:
            vlog(f"[awdb-soap] last response snippet: {last_response_snippet}")

    # ---------- Per-element fallback when we have stations but no usable series ----------
    # Some deployments omit <elementCd> and pack daily values into flat <values> with separate begin/end dates.
    # Re-query one element at a time and map flat values to daily dates.
    if stations_set and all(k[1] in (None, "", "__UNKNOWN__") or not buckets[k] for k in buckets.keys()):
        vlog("[awdb-soap] per-element fallback: re-querying SNWD/WTEQ/PREC separately")

        def _per_element_request(elem_code: str, use_ordinal: bool):
            bodies = []
            if not use_ordinal:
                bodies.append((
                    "v11-noaction-per-elt",
                    f"""\
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soapenv:Header/>
  <soapenv:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      <elementCd>{elem_code}</elementCd>
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <ordinal xsi:nil="true"/>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soapenv:Body>
</soapenv:Envelope>
""".encode("utf-8"),
                    {
                        "Content-Type": "text/xml; charset=utf-8",
                        "User-Agent": "SnowAPI/1.0 (+you@domain)",
                    }
                ))
            else:
                bodies.append((
                    "v11-noaction-ord1-per-elt",
                    f"""\
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:awdb="http://www.wcc.nrcs.usda.gov/ns/awdbWebService"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <soapenv:Header/>
  <soapenv:Body>
    <awdb:getData>
      {"".join(f"<stationTriplets>{t}</stationTriplets>" for t in triplets)}
      <elementCd>{elem_code}</elementCd>
      <getFlags>false</getFlags>
      <alwaysReturnDailyFeb29>false</alwaysReturnDailyFeb29>
      <duration>DAILY</duration>
      <heightDepth>-1</heightDepth>
      <ordinal>1</ordinal>
      <beginDate>{begin:%Y-%m-%d}</beginDate>
      <endDate>{end:%Y-%m-%d}</endDate>
    </awdb:getData>
  </soapenv:Body>
</soapenv:Envelope>
""".encode("utf-8"),
                    {
                        "Content-Type": "text/xml; charset=utf-8",
                        "User-Agent": "SnowAPI/1.0 (+you@domain)",
                    }
                ))
            series = []
            for variant_name, body, headers in bodies:
                try:
                    r = requests.post(SOAP_URL, data=body, headers=headers, timeout=REQUEST_TIMEOUT)
                    try:
                        snippet = r.text[:600].replace("\n"," ")
                    except Exception:
                        snippet = str(r.content[:600])
                    vlog(f"[awdb-soap] POST {SOAP_URL} ({variant_name}) status={r.status_code}")
                    if r.status_code != 200:
                        vlog(f"[awdb-soap] ({variant_name}) non-200 body (first 600 chars): {snippet}")
                        continue
                    try:
                        xml2 = ET.fromstring(r.content)
                    except ET.ParseError:
                        vlog(f"[awdb-soap] ({variant_name}) could not parse XML; body (first 600 chars): {snippet}")
                        continue
                    for ret2 in xml2.iter():
                        if _strip_ns(ret2.tag) != "return":
                            continue
                        t_trip, begin_dt, end_dt = None, None, None
                        flat_vals = []
                        for child in list(ret2):
                            tag = _strip_ns(child.tag)
                            if tag == "stationTriplet":
                                t_trip = (child.text or "").strip()
                            elif tag == "beginDate":
                                try:
                                    begin_dt = dtparse.isoparse((child.text or "").strip()).date()
                                except Exception:
                                    begin_dt = None
                            elif tag == "endDate":
                                try:
                                    end_dt = dtparse.isoparse((child.text or "").strip()).date()
                                except Exception:
                                    end_dt = None
                            elif tag == "values":
                                if list(child):
                                    # nested values handled by main parser
                                    pass
                                else:
                                    txt = (child.text or "").strip()
                                    if txt != "":
                                        try:
                                            flat_vals.append(float(txt))
                                        except ValueError:
                                            pass
                        if t_trip and flat_vals and begin_dt and end_dt:
                            series.append((t_trip, elem_code, flat_vals, begin_dt, end_dt))
                except Exception:
                    continue
            return series

        filled_before = sum(len(v) for v in buckets.values())
        for code in ("SNWD","WTEQ","PREC"):
            ser = _per_element_request(code, use_ordinal=False)
            if not ser:
                ser = _per_element_request(code, use_ordinal=True)
            for t_trip, code2, flat_vals, bdt, edt in ser:
                try:
                    num_days = (edt - bdt).days + 1
                    seq = flat_vals[:num_days]
                    for i, v in enumerate(seq):
                        d = bdt + timedelta(days=i)
                        days_set.add(d)
                        key = (t_trip, code2)
                        if key not in buckets:
                            buckets[key] = {}
                        buckets[key][d] = float(v) if v is not None else None
                except Exception:
                    pass
        filled_after = sum(len(v) for v in buckets.values())
        if VERBOSE:
            vlog(f"[awdb-soap] per-element fallback filled {filled_after - filled_before} day-values across {len(buckets)} series")
    # ---------- end per-element fallback ----------

    # Map to output in centimeters AND capture raw inches
    out_cm = []
    out_raw = []
    # Strip placeholder element keys
    cleaned = {}
    for (trip, elem), series in buckets.items():
        if elem in (None, "", "__UNKNOWN__"):
            # ignore unknown element buckets; they shouldn't have values unless fallback filled
            continue
        cleaned[(trip, elem)] = series

    trips_present = {k[0] for k in cleaned.keys()}
    all_days = set()
    for series in cleaned.values():
        all_days.update(series.keys())
    for trip in trips_present:
        for d in sorted(all_days):
            snwd_in = (cleaned.get((trip, "SNWD"), {}) or {}).get(d)
            wteq_in = (cleaned.get((trip, "WTEQ"), {}) or {}).get(d)
            prec_in = (cleaned.get((trip, "PREC"), {}) or {}).get(d)
            # if all None, skip row
            if snwd_in is None and wteq_in is None and prec_in is None:
                continue
            out_raw.append({
                "station_triplet": trip,
                "date": d,
                "snwd_in": snwd_in,
                "wteq_in": wteq_in,
                "prec_in": prec_in,
            })
            out_cm.append({
                "station_triplet": trip,
                "date": d,
                "snow_depth_cm": snwd_in * IN_TO_CM if snwd_in is not None else None,
                "swe_cm":        wteq_in * IN_TO_CM if wteq_in is not None else None,
                "precip_cm":     prec_in * IN_TO_CM if prec_in is not None else None,
            })
    return out_cm, out_raw



# --- SNOTEL daily obs (SNWD/WTEQ/PREC) helpers ---
def _awdb_get(url: str, params: dict, headers: dict | None = None, timeout=REQUEST_TIMEOUT):
    headers = headers or {"User-Agent": "SnowAPI/1.0 (+you@domain)"}
    last_err = None
    for attempt in range(1, AWDB_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            vlog(f"[awdb] GET {r.url} attempt={attempt} status={r.status_code}")
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            # bubble 404s to the splitter; retry on 5xx/429/timeouts
            code = getattr(e.response, "status_code", None)
            if code == 404:
                raise
            last_err = e
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
        # backoff then retry
        time.sleep(AWDB_BACKOFF * attempt)
    # all retries exhausted
    if last_err:
        raise last_err


def _fetch_awdb_daily_obs_raw(triplets: list[str], begin: date, end: date) -> tuple[list[dict], list[dict]]:
    """
    Low-level fetch (SOAP). Returns a tuple (rows_cm, rows_raw_in).
    Uses AWDB SOAP getData with SNWD, WTEQ, PREC (English inches -> cm).
    """
    if not triplets:
        return ([], [])
    # Directly use SOAP because REST does not expose getDailyData.
    return _awdb_get_daily_via_soap(triplets, begin, end)


def fetch_awdb_daily_obs_for_triplets(triplets: list[str], begin: date, end: date) -> tuple[list[dict], list[dict]]:
    if not triplets:
        return ([], [])
    try:
        return _fetch_awdb_daily_obs_raw(triplets, begin, end)
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status != 404:
            raise
        if len(triplets) == 1:
            print(f"[obs] no data for {triplets[0]} in range; treating as empty", flush=True)
            return ([], [])
        mid = len(triplets) // 2
        print(f"[obs] 404 on batch of {len(triplets)}; splitting into {len(triplets[:mid])}+{len(triplets[mid:])}", flush=True)
        left_cm, left_raw = fetch_awdb_daily_obs_for_triplets(triplets[:mid], begin, end)
        right_cm, right_raw = fetch_awdb_daily_obs_for_triplets(triplets[mid:], begin, end)
        return (left_cm + right_cm, left_raw + right_raw)

#
# DB SCHEMA NOTE:
# Create this table once:
#   CREATE TABLE IF NOT EXISTS snotel_daily_raw (
#     station_triplet TEXT NOT NULL,
#     date DATE NOT NULL,
#     snwd_in DOUBLE PRECISION,
#     wteq_in DOUBLE PRECISION,
#     prec_in DOUBLE PRECISION,  -- typically cumulative precipitation
#     source TEXT,
#     PRIMARY KEY (station_triplet, date)
#   );
#
# You can derive daily precipitation with a view like:
#   CREATE OR REPLACE VIEW snotel_daily_precip_cm AS
#   SELECT r.station_triplet, r.date,
#          GREATEST(0, (r.prec_in - LAG(r.prec_in) OVER (PARTITION BY r.station_triplet ORDER BY r.date))) * 2.54
#            AS precip_cm_daily
#   FROM snotel_daily_raw r;
def upsert_snotel_daily_obs(states: list[str] | None, days_back: int = 14):
    end = date.today()
    begin = end - timedelta(days=days_back)
    state_filter = {s.upper() for s in states} if states else None
    with pg() as conn, conn.cursor() as cur:
        if state_filter:
            cur.execute("""
                SELECT station_triplet
                FROM snotel_station
                WHERE split_part(station_triplet, ':', 2) = ANY(%s)
            """, (list(state_filter),))
        else:
            cur.execute("SELECT station_triplet FROM snotel_station")
        trips = [r[0] for r in cur.fetchall()]
    if not trips:
        print("[obs] no stations found to fetch", file=sys.stderr, flush=True)
        return
    total_chunks = (len(trips) + CHUNK_SIZE - 1) // CHUNK_SIZE
    print(f"[obs] fetching daily SNWD/WTEQ/PREC for {len(trips)} stations {begin}..{end} (chunk_size={CHUNK_SIZE}, total_chunks={total_chunks})", flush=True)

    total = 0
    with pg() as conn, conn.cursor() as cur:
        for idx, i in enumerate(range(0, len(trips), CHUNK_SIZE), start=1):
            chunk = trips[i:i+CHUNK_SIZE]
            print(f"[obs] chunk {idx}/{total_chunks} — triplets {i+1}..{i+len(chunk)}", flush=True)
            try:
                rows_cm, rows_raw = fetch_awdb_daily_obs_for_triplets(chunk, begin, end)
            except requests.HTTPError as e:
                print(f"[obs] HTTP error on chunk {idx}: {e}", file=sys.stderr, flush=True)
                continue
            if not rows_cm and not rows_raw:
                print(f"[obs] chunk {idx}: 0 rows (all invalid?)", flush=True)
                continue
            if rows_raw:
                cur.executemany("""
                    INSERT INTO snotel_daily_raw
                      (station_triplet, date, snwd_in, wteq_in, prec_in, source)
                    VALUES (%(station_triplet)s, %(date)s, %(snwd_in)s, %(wteq_in)s, %(prec_in)s, 'awdb')
                    ON CONFLICT (station_triplet, date) DO UPDATE SET
                      snwd_in = EXCLUDED.snwd_in,
                      wteq_in = EXCLUDED.wteq_in,
                      prec_in = EXCLUDED.prec_in,
                      source  = EXCLUDED.source
                """, rows_raw)
            if rows_cm:
                cur.executemany("""
                    INSERT INTO snotel_daily_obs
                      (station_triplet, date, snow_depth_cm, swe_cm, precip_cm, source)
                    VALUES (%(station_triplet)s, %(date)s, %(snow_depth_cm)s, %(swe_cm)s, %(precip_cm)s, 'awdb')
                    ON CONFLICT (station_triplet, date) DO UPDATE SET
                      snow_depth_cm = EXCLUDED.snow_depth_cm,
                      swe_cm        = EXCLUDED.swe_cm,
                      precip_cm     = EXCLUDED.precip_cm,
                      source        = EXCLUDED.source
                """, rows_cm)
def create_reporting_views():
    with pg() as conn, conn.cursor() as cur:
        # Daily liquid precip (cm) from cumulative PREC inches
        cur.execute("""
            CREATE OR REPLACE VIEW snotel_daily_precip_cm AS
            SELECT
                station_triplet,
                date,
                GREATEST(0, (prec_in - LAG(prec_in) OVER (PARTITION BY station_triplet ORDER BY date))) * 2.54
                    AS precip_cm_daily
            FROM snotel_daily_raw;
        """)
        # Daily new snow (cm) as non-negative change in SNWD (inches)
        cur.execute("""
            CREATE OR REPLACE VIEW snotel_daily_new_snow_cm AS
            SELECT
                station_triplet,
                date,
                GREATEST(0, (snwd_in - LAG(snwd_in) OVER (PARTITION BY station_triplet ORDER BY date))) * 2.54
                    AS new_snow_cm
            FROM snotel_daily_raw;
        """)
        # Daily SWE gain (cm) as non-negative change in WTEQ (inches)
        cur.execute("""
            CREATE OR REPLACE VIEW snotel_daily_swe_gain_cm AS
            SELECT
                station_triplet,
                date,
                GREATEST(0, (wteq_in - LAG(wteq_in) OVER (PARTITION BY station_triplet ORDER BY date))) * 2.54
                    AS swe_gain_cm
            FROM snotel_daily_raw;
        """)
        # Convenience combined view
        cur.execute("""
            CREATE OR REPLACE VIEW snotel_daily_accums_cm AS
            SELECT
                r.station_triplet,
                r.date,
                n.new_snow_cm,
                s.swe_gain_cm,
                p.precip_cm_daily
            FROM (SELECT DISTINCT station_triplet, date FROM snotel_daily_raw) r
            LEFT JOIN snotel_daily_new_snow_cm n USING (station_triplet, date)
            LEFT JOIN snotel_daily_swe_gain_cm s USING (station_triplet, date)
            LEFT JOIN snotel_daily_precip_cm p USING (station_triplet, date);
        """)
    print("[views] (re)created snotel_daily_* views")

def fetch_and_store_openmeteo_model(model: str, lat: float, lon: float, station_triplet: str | None = None, days_ahead: int | None = None):
    endpoint = OM_ENDPOINTS.get(model)
    if not endpoint:
        print(f"[forecast] unknown model '{model}', skipping")
        return 0
    params = {"latitude": lat, "longitude": lon, "hourly": "snowfall", "timezone": TZ}
    # Optional horizon limiting
    if days_ahead is not None:
        start_d = date.today()
        end_d = start_d + timedelta(days=days_ahead)
        params["start_date"] = start_d.strftime("%Y-%m-%d")
        params["end_date"] = end_d.strftime("%Y-%m-%d")
    r = requests.get(endpoint, params=params, timeout=60)
    vlog(f"[forecast] {model} GET {r.url} status={r.status_code}")
    r.raise_for_status()
    data = r.json()
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    snows = hourly.get("snowfall", [])
    ts_forecast = datetime.now(timezone.utc)
    if not times or not snows:
        print(f"[{model}] no hourly snowfall returned", file=sys.stderr)
        return 0
    recs = []
    for t, s in zip(times, snows):
        ts_valid = dtparse.isoparse(t)
        if ts_valid.tzinfo is None:
            ts_valid = ts_valid.replace(tzinfo=timezone.utc)
        recs.append((
            model,
            ts_forecast,
            ts_valid,
            lat,
            lon,
            float(s) if s is not None else None,
            "open-meteo",
            station_triplet
        ))
    with pg() as conn, conn.cursor() as cur:
        cur.executemany("""
            INSERT INTO forecast_hourly
              (model_name, ts_forecast, ts_valid, latitude, longitude, snowfall_cm, source, station_triplet)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (model_name, ts_forecast, ts_valid, latitude, longitude)
            DO UPDATE SET snowfall_cm = EXCLUDED.snowfall_cm,
                          source = EXCLUDED.source,
                          station_triplet = COALESCE(EXCLUDED.station_triplet, forecast_hourly.station_triplet)
        """, recs)
    print(f"[{model}] upserted {len(recs)} hourly rows for ({lat},{lon}) triplet={station_triplet or '-'}")
    return len(recs)

def forecast_for_stations(models: list[str], states: list[str] | None = None, days_ahead: int | None = None):
    state_filter = {s.upper() for s in states} if states else None
    with pg() as conn, conn.cursor() as cur:
        if state_filter:
            cur.execute("""
                SELECT station_triplet, latitude, longitude
                FROM snotel_station
                WHERE split_part(station_triplet, ':', 2) = ANY(%s)
            """, (list(state_filter),))
        else:
            cur.execute("SELECT station_triplet, latitude, longitude FROM snotel_station")
        rows = cur.fetchall()
    print(f"[forecast] running for {len(rows)} stations across models={models}")
    for trip, lat, lon in rows:
        for m in models:
            try:
                fetch_and_store_openmeteo_model(m, lat, lon, station_triplet=trip, days_ahead=days_ahead)
            except Exception as e:
                print(f"[forecast] {trip} {m} ERROR: {e}", file=sys.stderr)


# Orchestrator: stations -> obs -> forecasts
def cmd_update_all(days_back: int = 14, models: list[str] | None = None, states: list[str] | None = None):
    start_ts = time.time()
    print(f"[update-all] start (days_back={days_back}, models={models or MODELS}, states={states or '-'})", flush=True)
    try:
        # 1) Stations
        upsert_snotel_metadata(states=states)
        # 2) Observations
        upsert_snotel_daily_obs(states or None, days_back=days_back)
        # 3) Forecasts for every station
        forecast_for_stations(models or MODELS, states or None)
    finally:
        print(f"[update-all] done in {time.time() - start_ts:0.1f}s", flush=True)

if __name__ == "__main__":
    import argparse
    def forecast_loop(lat: float, lon: float, models: list[str], interval_sec: int):
        while True:
            try:
                for m in models:
                    fetch_and_store_openmeteo_model(m, lat, lon)
            except Exception as e:
                print(f"[forecast] ERROR: {e}", file=sys.stderr)
            time.sleep(interval_sec)

    parser = argparse.ArgumentParser(description="SnowAPI task runner")
    # NEW: runtime verbose toggle
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose logging (overrides VERBOSE env)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_st = sub.add_parser("stations", help="Upsert SNOTEL station metadata")
    p_st.add_argument("--states", default=os.getenv("SNOTEL_STATES", "CO,UT,WY,ID,MT,WA,OR,CA,NV,NM,AZ"))

    p_od = sub.add_parser("obs-daily", help="Fetch & upsert SNOTEL daily obs (SNWD/WTEQ/PREC)")
    p_od.add_argument("--states", default=os.getenv("SNOTEL_STATES", ""))
    p_od.add_argument("--days-back", type=int, default=int(os.getenv("OBS_DAYS_BACK", "14")))

    # NEW: single-triplet probe/fetch (optional upsert)
    p_od1 = sub.add_parser("obs-daily-triplet", help="Fetch & optionally upsert SNOTEL daily obs for a single station")
    p_od1.add_argument("triplet", help="Station triplet like 663:CO:SNTL")
    p_od1.add_argument("--days-back", type=int, default=14)
    p_od1.add_argument("--upsert", action="store_true", help="Write results into snotel_daily_obs")

    p_fs = sub.add_parser("forecast-stations", help="Fetch snowfall for all SNOTEL stations")
    p_fs.add_argument("--models", default=os.getenv("MODELS", "gfs,icon,ecmwf"))
    p_fs.add_argument("--states", default=os.getenv("SNOTEL_STATES", ""))
    p_fs.add_argument("--days-ahead", type=int, default=FORECAST_DAYS_AHEAD, help="Limit forecast horizon (days ahead).")

    p_fo = sub.add_parser("forecast-once", help="Fetch snowfall for all models once")
    p_fo.add_argument("--lat", type=float, default=float(os.getenv("LAT", "39.2")))
    p_fo.add_argument("--lon", type=float, default=float(os.getenv("LON", "-106.9")))
    p_fo.add_argument("--models", default=os.getenv("MODELS", "gfs,icon,ecmwf"))
    p_fo.add_argument("--days-ahead", type=int, default=FORECAST_DAYS_AHEAD, help="Limit forecast horizon (days ahead).")

    p_fl = sub.add_parser("forecast-loop", help="Continuously fetch snowfall on interval")
    p_fl.add_argument("--lat", type=float, default=float(os.getenv("LAT", "39.2")))
    p_fl.add_argument("--lon", type=float, default=float(os.getenv("LON", "-106.9")))
    p_fl.add_argument("--models", default=os.getenv("MODELS", "gfs,icon,ecmwf"))
    p_fl.add_argument("--interval", type=int, default=int(os.getenv("FETCH_INTERVAL_SEC", "10800")))

    p_probe = sub.add_parser("probe-soap", help="Probe SOAP for one station and print a body snippet")
    p_probe.add_argument("triplet", help="Station triplet like 663:CO:SNTL")
    p_probe.add_argument("--days-back", type=int, default=3)

    p_upall = sub.add_parser("update-all", help="Run stations -> obs-daily -> forecast-stations")
    p_upall.add_argument("--days-back", type=int, default=int(os.getenv("OBS_DAYS_BACK", "14")))
    p_upall.add_argument("--models", default=os.getenv("MODELS", "gfs,icon,ecmwf"))
    p_upall.add_argument("--states", default=os.getenv("SNOTEL_STATES", ""), help="Optional comma list to limit by state codes")
    p_upall.add_argument("--forecast-days-ahead", type=int, default=FORECAST_DAYS_AHEAD, help="Horizon (days ahead) to use when fetching forecasts.")

    p_views = sub.add_parser("init-views", help="Create or replace reporting views for daily accumulations")

    args = parser.parse_args()

    # NEW: honor -v/--verbose at runtime
    if getattr(args, "verbose", False):
        globals()["VERBOSE"] = True

    if args.cmd == "stations":
        states = [s.strip() for s in args.states.split(',') if s.strip()]
        upsert_snotel_metadata(states=states)

    elif args.cmd == "obs-daily":
        states = [s.strip() for s in args.states.split(',') if s.strip()]
        upsert_snotel_daily_obs(states or None, days_back=args.days_back)

    # NEW: single triplet flow
    elif args.cmd == "obs-daily-triplet":
        end = date.today()
        begin = end - timedelta(days=args.days_back)
        rows, raw_rows = _awdb_get_daily_via_soap([args.triplet], begin, end)
        print(f"[obs-one] fetched {len(rows)} rows for {args.triplet} {begin}..{end}")
        if rows[:5]:
            print("[obs-one] sample:", rows[:5])
        if raw_rows[:5]:
            print("[obs-one] raw sample (inches):", raw_rows[:5])
        if args.upsert and (rows or raw_rows):
            with pg() as conn, conn.cursor() as cur:
                if raw_rows:
                    cur.executemany("""
                        INSERT INTO snotel_daily_raw
                          (station_triplet, date, snwd_in, wteq_in, prec_in, source)
                        VALUES (%(station_triplet)s, %(date)s, %(snwd_in)s, %(wteq_in)s, %(prec_in)s, 'awdb')
                        ON CONFLICT (station_triplet, date) DO UPDATE SET
                          snwd_in = EXCLUDED.snwd_in,
                          wteq_in = EXCLUDED.wteq_in,
                          prec_in = EXCLUDED.precin,
                          source  = EXCLUDED.source
                    """, raw_rows)
                if rows:
                    cur.executemany("""
                        INSERT INTO snotel_daily_obs
                          (station_triplet, date, snow_depth_cm, swe_cm, precip_cm, source)
                        VALUES (%(station_triplet)s, %(date)s, %(snow_depth_cm)s, %(swe_cm)s, %(precip_cm)s, 'awdb')
                        ON CONFLICT (station_triplet, date) DO UPDATE SET
                          snow_depth_cm = EXCLUDED.snow_depth_cm,
                          swe_cm        = EXCLUDED.swe_cm,
                          precip_cm     = EXCLUDED.precip_cm,
                          source        = EXCLUDED.source
                    """, rows)
            print(f"[obs-one] upserted {len(rows)} rows (metric) and {len(raw_rows)} raw rows")

    elif args.cmd == "forecast-stations":
        models = [m.strip().lower() for m in args.models.split(',') if m.strip()]
        states = [s.strip() for s in args.states.split(',') if s.strip()]
        forecast_for_stations(models, states or None, days_ahead=getattr(args, "days_ahead", None))

    elif args.cmd == "forecast-once":
        models = [m.strip().lower() for m in args.models.split(',') if m.strip()]
        for m in models:
            fetch_and_store_openmeteo_model(m, args.lat, args.lon, days_ahead=getattr(args, "days_ahead", None))

    elif args.cmd == "forecast-loop":
        models = [m.strip().lower() for m in args.models.split(',') if m.strip()]
        forecast_loop(args.lat, args.lon, models, args.interval)

    elif args.cmd == "probe-soap":
        _awdb_probe_soap(args.triplet, args.days_back)

    elif args.cmd == "update-all":
        models = [m.strip().lower() for m in args.models.split(',') if m.strip()]
        states = [s.strip() for s in args.states.split(',') if s.strip()]
        cmd_update_all(days_back=args.days_back, models=models or None, states=states or None)
        forecast_for_stations(models or MODELS, states or None, days_ahead=getattr(args, "forecast_days_ahead", FORECAST_DAYS_AHEAD))

    elif args.cmd == "init-views":
        create_reporting_views()
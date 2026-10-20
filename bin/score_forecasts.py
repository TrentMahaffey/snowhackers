#!/usr/bin/env python3
"""Score the weather models' day-ahead snow forecasts against what SNOTEL observed, per zone.

For every zone station and local day D: each model's forecast for D as known before D began (the
newest run fetched in the 30 h before local midnight), vs observed new snow = SNOTEL snow-depth
increase from D-1 to D. Depth increase under-counts snow that settles, so this is a conservative
"what accumulated" truth; it is the same for every model, so model-vs-model comparisons are fair.

Scored over the last N days (default: since 2025-11-01) on "event days" (any model or the
observation >= 1"). Output snowdash/dashboard/data/forecast_skill.json:
  per zone: per model {mae, bias, n, hit_rate, false_alarm_rate}, best model, and normalized
  inverse-MAE weights that build_forecast.py uses for its skill-weighted best estimate.
Also scores our own published forecasts (forecast_archive/) once they have history.

Weekly cron + after big storms. Usage: python3 bin/score_forecasts.py [--since 2025-11-01]
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_forecast import ZONES, MODELS, psql_csv, nearest_stations, LOCAL  # noqa: E402

DATA = Path("/home/trent/snowhackers/snowdash/dashboard/data")
OUT = DATA / "forecast_skill.json"
EVENT_IN = 1.0
HIT_IN = 2.0


def month_ranges(start, end):
    d = date(start.year, start.month, 1)
    while d < end:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        yield max(d, start), min(nxt, end)
        d = nxt


def lead1_forecasts(triplet, start, end):
    """{(model, local_date): inches} for the day-ahead forecast."""
    res = {}
    for a, b in month_ranges(start, end):
        t = triplet.replace("'", "")
        sql = (f"select model_name, (ts_valid at time zone 'America/Denver')::date, sum(snowfall_cm) from ("
               f" select distinct on (model_name, ts_valid) model_name, ts_valid, snowfall_cm from forecast_hourly"
               f" where station_triplet = '{t}' and ts_valid >= '{a}' and ts_valid < '{b}'"
               f" and ts_forecast < date_trunc('day', ts_valid at time zone 'America/Denver') at time zone 'America/Denver'"
               f" and ts_forecast > (date_trunc('day', ts_valid at time zone 'America/Denver') at time zone 'America/Denver') - interval '30 hours'"
               f" order by model_name, ts_valid, ts_forecast desc) h group by 1, 2")
        for m, d, cm in psql_csv(sql):
            if cm:
                res[(m, date.fromisoformat(d))] = float(cm) / 2.54
    return res


def observed_new_snow(triplets, start, end):
    q = ",".join("'" + t.replace("'", "") + "'" for t in triplets)
    rows = psql_csv(f"select station_triplet, date, snow_depth_cm from snotel_daily_obs where station_triplet in ({q}) "
                    f"and date >= '{start - timedelta(days=1)}' and date < '{end}' and snow_depth_cm is not null order by 1, 2")
    depth = defaultdict(dict)
    for t, d, cm in rows:
        depth[t][date.fromisoformat(d)] = float(cm) / 2.54
    obs = {}
    for t, series in depth.items():
        for d, v in series.items():
            prev = series.get(d - timedelta(days=1))
            if prev is not None:
                obs[(t, d)] = max(0.0, v - prev)
    return obs


SPLIT = date(2026, 2, 15)
SCALE_MIN, SCALE_MAX = 0.7, 2.5


def fit_scale(pairs):
    f = sum(x for x, _ in pairs)
    o = sum(y for _, y in pairs)
    if f < 5 or len(pairs) < 10:
        return 1.0
    return round(min(SCALE_MAX, max(SCALE_MIN, o / f)), 2)


def corrected_estimate(fs, scales, weights):
    return sum(weights[m] * scales[m] * fs[m] for m in fs) / sum(weights[m] for m in fs)


def metrics(pairs):
    """pairs: [(forecast, observed)] on event days."""
    if not pairs:
        return None
    err = [f - o for f, o in pairs]
    hits = sum(1 for f, o in pairs if f >= HIT_IN and o >= HIT_IN)
    obs_events = sum(1 for _, o in pairs if o >= HIT_IN)
    fc_events = sum(1 for f, _ in pairs if f >= HIT_IN)
    return {"n": len(pairs), "mae": round(sum(abs(e) for e in err) / len(err), 2), "bias": round(sum(err) / len(err), 2),
            "hit_rate": round(hits / obs_events, 2) if obs_events else None,
            "false_alarm_rate": round((fc_events - hits) / fc_events, 2) if fc_events else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2025-11-01")
    args = ap.parse_args()
    start, end = date.fromisoformat(args.since), datetime.now(LOCAL).date()
    srows = psql_csv("select station_triplet, name, latitude, longitude from snotel_station where latitude is not null")
    near = nearest_stations([(t, n, float(la), float(lo)) for t, n, la, lo in srows])
    zone_st = {zid: sorted({t for p in ps for t, _, _ in near.get(p, [])}) for zid, _, ps in ZONES}
    all_st = sorted({t for v in zone_st.values() for t in v})
    obs = observed_new_snow(all_st, start, end)
    print(f"observations: {len(obs)} station-days")

    fc = {}
    for i, t in enumerate(all_st):
        fc[t] = lead1_forecasts(t, start, end)
        if i % 10 == 0:
            print(f"  forecasts {i + 1}/{len(all_st)}")

    # 2026-10-12: per-zone scale factors were tested (fit Nov-Feb, test Feb-May) and made the
    # forecast WORSE in 13/14 zones, so they are stored as diagnostics only and not applied.
    out = {"generated_at": datetime.now(LOCAL).isoformat(timespec="seconds"), "since": args.since,
           "scales_applied": False,
           "event_threshold_in": EVENT_IN, "zones": {}}
    allpairs = defaultdict(list)
    for zid, zname, _ in ZONES:
        pairs = defaultdict(list)
        dated = []
        for t in zone_st[zid]:
            days = {d for (tt, d) in obs if tt == t}
            for d in days:
                o = obs[(t, d)]
                fs = {m: fc[t].get((m, d), 0.0) for m in MODELS}
                if max(list(fs.values()) + [o]) < EVENT_IN:
                    continue
                for m, f in fs.items():
                    pairs[m].append((f, o))
                pairs["ensemble"].append((sum(fs.values()) / len(fs), o))
                dated.append((d, fs, o))
        z = {m: metrics(pairs[m]) for m in list(MODELS) + ["ensemble"]}
        scored = {m: v for m, v in z.items() if m in MODELS and v and v["n"] >= 10}
        if scored:
            inv = {m: 1 / max(v["mae"], 0.3) for m, v in scored.items()}
            tot = sum(inv.values())
            weights = {m: round(w / tot, 3) for m, w in inv.items()}
            best = min(scored, key=lambda m: scored[m]["mae"])
        else:
            weights, best = {m: round(1 / len(MODELS), 3) for m in MODELS}, None
        # Scale factors fit on ALL event days (production) ...
        scales = {m: fit_scale(pairs[m]) for m in MODELS}
        # ... and an honest check: fit before SPLIT, evaluate after it.
        tr = [x for x in dated if x[0] < SPLIT]
        te = [x for x in dated if x[0] >= SPLIT]
        check = None
        if len(tr) >= 20 and len(te) >= 20:
            tr_sc = {m: fit_scale([(fs[m], o) for _, fs, o in tr]) for m in MODELS}
            eq = {m: 1.0 for m in MODELS}
            raw = [abs(sum(fs.values()) / len(fs) - o) for _, fs, o in te]
            cor = [abs(corrected_estimate(fs, tr_sc, eq) - o) for _, fs, o in te]
            check = {"n_test": len(te), "raw_mae": round(sum(raw) / len(raw), 2), "corrected_mae": round(sum(cor) / len(cor), 2),
                     "train_scales": tr_sc}
        out["zones"][zid] = {"name": zname, "models": z, "best_model": best, "weights": weights,
                             "scales": scales, "holdout_check": check}
        if check:
            print(f"      scale check {zid}: raw {check['raw_mae']} -> corrected {check['corrected_mae']} (n={check['n_test']}) scales {scales}")
        for m in list(MODELS) + ["ensemble"]:
            allpairs[m] += pairs[m]
        bm = z.get(best) if best else None
        print(f"  {zid:16s} best={best or '-':6s} " + "  ".join(
            f"{m} {z[m]['mae']:.2f}/{z[m]['bias']:+.2f}" for m in list(MODELS) + ["ensemble"] if z[m]))
    out["overall"] = {m: metrics(allpairs[m]) for m in list(MODELS) + ["ensemble"]}

    # Our own published forecasts (day 1 = first daily value of each archived forecast).
    arch = sorted((DATA / "forecast_archive").glob("*.json"))
    own = defaultdict(list)
    for f in arch:
        d = json.loads(f.read_text())
        for z in d.get("zones", []):
            day1 = z["forecast"]["daily"][0]
            dd = date.fromisoformat(day1["date"])
            os_ = [obs[(t, dd)] for t in zone_st.get(z["id"], []) if (t, dd) in obs]
            if os_:
                o = sum(os_) / len(os_)
                if max(day1["mean"], o) >= EVENT_IN:
                    own[z["id"]].append((day1.get("best", day1["mean"]), o))
    out["published"] = {zid: metrics(p) for zid, p in own.items()}

    DATA.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(OUT)
    print("overall:", {m: (v["mae"], v["bias"]) for m, v in out["overall"].items() if v})
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()

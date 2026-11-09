"""Cam registry: loads cams.json, the single source of truth for snow cams.

Each cam: prefix (capture filename prefix), name (display / resort-report name), region,
lat/lon (for nearest SNOTEL), zone (forecast zone id), and, for cams the stake-reading
model runs on, model = {stake_type, label, note, adapter?}.

cam_config.json and cam_routing.json are GENERATED from it (the Blackwell prediction
scripts and snowcammeasurement/finetune/build_dataset.py read those):

    python3 bin/camreg.py export        # rewrite both files in place
    python3 bin/camreg.py check         # exit 1 if they are out of date
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = [ROOT / "cams.json", Path("/snowcam-config/cams.json"), Path("/app/bin/../cams.json")]


def _path():
    for p in CANDIDATES:
        if p.exists():
            return p
    raise FileNotFoundError("cams.json not found in " + ", ".join(map(str, CANDIDATES)))


_cache = {}


def load():
    p = _path()
    m = p.stat().st_mtime
    if _cache.get("key") != (p, m):
        _cache.update(key=(p, m), data=json.loads(p.read_text()))
    return _cache["data"]


def cams():
    return load()["cams"]


def names():
    """{prefix: display name}"""
    return {c["prefix"]: c["name"] for c in cams()}


def coords():
    """{prefix: (lat, lon)} for cams with known coordinates."""
    return {c["prefix"]: (c["lat"], c["lon"]) for c in cams() if "lat" in c}


def zones():
    """[(zone_id, zone_name, [prefixes])] in registry order."""
    d = load()
    return [(z["id"], z["name"], [c["prefix"] for c in d["cams"] if c.get("zone") == z["id"]]) for z in d["zones"]]


def predict_config():
    """cam_config.json contents: model cams as {resort, prefix, stake_type, resort_label, resort_note}."""
    out = []
    for c in cams():
        m = c.get("model")
        if not m:
            continue
        e = {"resort": c["name"], "prefix": c["prefix"], "stake_type": m["stake_type"],
             "resort_label": m["label"], "resort_note": m["note"]}
        if m.get("sherpa_rubric"):  # "pile" selects the run-11+ Sherpa prompt
            e["sherpa_rubric"] = m["sherpa_rubric"]
        out.append(e)
    return out


def routing():
    """cam_routing.json contents: {_default, _why, prefix: adapter} for cams with a non-default adapter."""
    d = load()
    out = {"_default": d["default_adapter"], "_why": d["adapter_notes"]}
    out.update({c["prefix"]: c["model"]["adapter"] for c in d["cams"] if c.get("model", {}).get("adapter")})
    return out


def _render(obj):
    return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def _targets():
    return {ROOT / "cam_config.json": _render(predict_config()), ROOT / "cam_routing.json": _render(routing())}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    stale = [p for p, s in _targets().items() if not p.exists() or p.read_text() != s]
    if cmd == "export":
        for p, s in _targets().items():
            if p in stale:
                with open(p, "w") as f:  # write in place: keeps the inode for any bind mounts
                    f.write(s)
        print("updated:", ", ".join(p.name for p in stale) or "nothing (already current)")
    elif cmd == "check":
        if stale:
            print("out of date (run: python3 bin/camreg.py export):", ", ".join(p.name for p in stale))
            sys.exit(1)
        print("cam_config.json and cam_routing.json are current")
    else:
        sys.exit(__doc__)

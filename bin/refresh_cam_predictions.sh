#!/usr/bin/env bash
# Refresh model predictions for all configured snow cams.
#
# Workflow:
#   0. Regenerate cam_config.json + cam_routing.json from cams.json (bin/camreg.py).
#   1. For each model cam in cam_config.json, pick the latest .jpg in
#      /home/trent/snowcamtimelapse/out/ matching the prefix.
#   2. rsync those images to the Blackwell (only newer/changed).
#   3. Sync predict_latest.py + cam_config.json + cam_routing.json to Blackwell.
#   4. Run predict_latest.py over SSH, once per adapter group (each cam's adapter
#      comes from cams.json; run 7 is the default), and merge.
#   5. Pull the predictions JSON back to the snowhackers mount.
#
# Cron: hourly. Output: /home/trent/snowhackers/cam_predictions.json
# (mounted into the snowdash container at /app/cam_predictions.json).

set -euo pipefail

BW_HOST="${BW_HOST:-192.168.0.117}"
BW_USER="${BW_USER:-trent-mahaffey}"
REMOTE_DIR="${REMOTE_DIR:-/home/$BW_USER/snowcam_predict}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_IMG_DIR="/home/trent/snowcamtimelapse/out"
LOCAL_FT_DIR="/home/trent/snowcammeasurement/finetune"
LOCAL_CONFIG="$HERE/cam_config.json"
LOCAL_OUT="$HERE/cam_predictions.json"
# Per-cam adapter routing: the `adapter` field in cams.json (why: finetune/TRAINING_LOGS.MD runs 9-10).

log() { echo "[$(date +%H:%M:%S)] $*"; }

# 0. Skip while a fine-tune is training on the shared Blackwell GPU (OOM risk).
if ssh "$BW_USER@$BW_HOST" "test -f /home/$BW_USER/snow_finetune/training.lock"; then
  log "Training in progress on Blackwell; skipping this hour"
  exit 0
fi

# 1. Pick latest image per resort prefix
log "Picking latest image per resort"
TMPLIST=$(mktemp)
python3 - "$LOCAL_CONFIG" "$LOCAL_IMG_DIR" >"$TMPLIST" <<'PY'
import json, sys, re
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text())
img_dir = Path(sys.argv[2])
def is_image(path):
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return head[:3] == b"\xff\xd8\xff" or head == b"\x89PNG\r\n\x1a\n"

for entry in config:
    pat = re.compile(re.escape(entry['prefix']) + r"_\d{8}_\d{6}(_\d+)?\.jpg$")
    cands = sorted(p.name for p in img_dir.glob(f"{entry['prefix']}_*.jpg") if pat.match(p.name))
    for name in reversed(cands):
        if is_image(img_dir / name):
            print(name)
            break
PY
log "  $(wc -l <"$TMPLIST") images to sync"

# 2. rsync the latest images + scripts to Blackwell
log "Rsyncing images to Blackwell"
ssh "$BW_USER@$BW_HOST" "mkdir -p $REMOTE_DIR/images"
rsync -a --files-from="$TMPLIST" "$LOCAL_IMG_DIR/" "$BW_USER@$BW_HOST:$REMOTE_DIR/images/"

python3 "$HERE/bin/camreg.py" export >/dev/null  # cam_config/cam_routing are generated from cams.json
log "Rsyncing predict_latest.py + cam_config.json"
rsync -a "$LOCAL_FT_DIR/predict_latest.py" "$BW_USER@$BW_HOST:$REMOTE_DIR/"
rsync -a "$LOCAL_CONFIG" "$BW_USER@$BW_HOST:$REMOTE_DIR/cam_config.json"
rsync -a "$HERE/cam_routing.json" "$BW_USER@$BW_HOST:$REMOTE_DIR/cam_routing.json"

# 3. Run inference on remote (uses venv from finetune workspace)
log "Running inference on Blackwell"
ssh "$BW_USER@$BW_HOST" bash <<EOSSH
set -euo pipefail
cd /home/$BW_USER/snowcam_predict
source /home/$BW_USER/snow_finetune/venv/bin/activate
python3 - <<'PY'
import json, os, subprocess
cfg = json.load(open("cam_config.json"))
route = json.load(open("cam_routing.json"))
groups = {}
for e in cfg:
    groups.setdefault(route.get(e["prefix"], route["_default"]), []).append(e)
missing = [a for a in groups if not os.path.isdir(f"/home/trent-mahaffey/snow_finetune/{a}")]
if missing:
    raise SystemExit(f"Adapter(s) not found in snow_finetune: {missing}")
got = {}
for i, (adapter, entries) in enumerate(groups.items()):
    json.dump(entries, open(f"cam_config_g{i}.json", "w"))
    subprocess.run(["python3", "predict_latest.py", "--adapter", f"/home/trent-mahaffey/snow_finetune/{adapter}",
                    "--config", f"cam_config_g{i}.json", "--img-dir", "images", "--out", f"cam_predictions_g{i}.json"], check=True)
    for r in json.load(open(f"cam_predictions_g{i}.json")):
        r["adapter"] = adapter.split("/")[-1]
        got[r["prefix"]] = r
out = [got[e["prefix"]] for e in cfg if e["prefix"] in got]
json.dump(out, open("cam_predictions.json", "w"), indent=2)
print(f"merged {len(out)} predictions from {len(groups)} adapters")
PY
EOSSH

# 4. Pull predictions back
log "Pulling predictions to $LOCAL_OUT"
# --inplace keeps the inode: snowdash bind-mounts this single file, and a rename-replace
# (rsync default) leaves the container reading the old copy until it restarts.
rsync -a --inplace "$BW_USER@$BW_HOST:$REMOTE_DIR/cam_predictions.json" "$LOCAL_OUT"

# 5. Add this hour's predictions to the per-image history so the search
#    UI and highlights get fresh rows (bin/history_db.py, table cam_prediction).
log "Adding latest predictions to the cam_prediction table"
python3 - "$LOCAL_OUT" "$HERE/bin" <<'PY'
import json, sys, re
from pathlib import Path

latest_path = Path(sys.argv[1])
sys.path.insert(0, sys.argv[2])
import history_db
latest = json.loads(latest_path.read_text())

# {prefix}_{YYYYMMDD}_{HHMMSS}[_MS].jpg
FN = re.compile(r"^(?P<prefix>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_\d+)?\.jpg$")

def ts_from(name):
    m = FN.match(name or "")
    if not m: return None
    d, t = m.group("date"), m.group("time")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:]}"

rows = []
for r in latest:
    if "image" not in r or "parsed" not in r:
        continue
    parsed = r.get("parsed") or {}
    if not parsed:
        continue
    prefix = r.get("prefix")
    name = r.get("image")
    stake_type = r.get("stake_type") or "depth_stake"
    if stake_type == "sherpa_bar":
        depth = parsed.get("snowfall_24h_inches")
        if depth is None: depth = parsed.get("snowfall_storm_inches")
    else:
        depth = parsed.get("depth_inches")
    row = {
        "prefix": prefix,
        "ts": ts_from(name),
        "name": name,
        "depth_inches": depth,
        "confidence": parsed.get("confidence"),
        "source": "model",
        "stake_visible": parsed.get("stake_visible"),
        "stake_type": stake_type,
        "predicted_at": r.get("predicted_at"),
    }
    if stake_type == "sherpa_bar":
        row["snowfall_24h_inches"] = parsed.get("snowfall_24h_inches")
        row["snowfall_storm_inches"] = parsed.get("snowfall_storm_inches")
    rows.append(row)

n = history_db.upsert(rows)  # never overwrites label rows
print(f"  Wrote {n} of {len(rows)} rows (label rows are kept)")
PY

rm -f "$TMPLIST"
log "Done. $(jq 'length' "$LOCAL_OUT" 2>/dev/null || echo "?") predictions written."

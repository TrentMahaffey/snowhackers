#!/usr/bin/env bash
# Refresh model predictions for all configured snow cams.
#
# Workflow:
#   1. For each resort in cam_config.json, pick the latest .jpg in
#      /home/trent/snowcamtimelapse/out/ matching the prefix.
#   2. rsync those images to the Blackwell (only newer/changed).
#   3. Sync the latest predict_latest.py + cam_config.json to Blackwell.
#   4. Run predict_latest.py over SSH.
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
ADAPTER="outputs/run_20260518_145633"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# 1. Pick latest image per resort prefix
log "Picking latest image per resort"
TMPLIST=$(mktemp)
python3 - "$LOCAL_CONFIG" "$LOCAL_IMG_DIR" >"$TMPLIST" <<'PY'
import json, sys
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text())
img_dir = Path(sys.argv[2])
for entry in config:
    cands = sorted(img_dir.glob(f"{entry['prefix']}_*.jpg"))
    if cands:
        print(cands[-1].name)
PY
log "  $(wc -l <"$TMPLIST") images to sync"

# 2. rsync the latest images + scripts to Blackwell
log "Rsyncing images to Blackwell"
ssh "$BW_USER@$BW_HOST" "mkdir -p $REMOTE_DIR/images"
rsync -a --files-from="$TMPLIST" "$LOCAL_IMG_DIR/" "$BW_USER@$BW_HOST:$REMOTE_DIR/images/"

log "Rsyncing predict_latest.py + cam_config.json"
rsync -a "$LOCAL_FT_DIR/predict_latest.py" "$BW_USER@$BW_HOST:$REMOTE_DIR/"
rsync -a "$LOCAL_CONFIG" "$BW_USER@$BW_HOST:$REMOTE_DIR/cam_config.json"

# 3. Run inference on remote (uses venv from finetune workspace)
log "Running inference on Blackwell"
ssh "$BW_USER@$BW_HOST" bash <<EOSSH
set -euo pipefail
cd /home/$BW_USER/snowcam_predict
# Resolve adapter path - it lives in the finetune workspace
ADAPTER_PATH=/home/$BW_USER/snow_finetune/$ADAPTER
if [ ! -d "\$ADAPTER_PATH" ]; then
  echo "Adapter not found at \$ADAPTER_PATH" >&2
  exit 1
fi
source /home/$BW_USER/snow_finetune/venv/bin/activate
python3 predict_latest.py \
  --adapter "\$ADAPTER_PATH" \
  --config cam_config.json \
  --img-dir images \
  --out cam_predictions.json
EOSSH

# 4. Pull predictions back
log "Pulling predictions to $LOCAL_OUT"
rsync -a "$BW_USER@$BW_HOST:$REMOTE_DIR/cam_predictions.json" "$LOCAL_OUT"

# 5. Append today's predictions to the per-image history file so the search
#    UI gets fresh rows even without running the full backfill.
LOCAL_HISTORY="$HERE/cam_predictions_history.json"
if [ -f "$LOCAL_HISTORY" ]; then
  log "Appending latest predictions to $LOCAL_HISTORY"
  python3 - "$LOCAL_OUT" "$LOCAL_HISTORY" <<'PY'
import json, sys, re
from pathlib import Path

latest_path, history_path = Path(sys.argv[1]), Path(sys.argv[2])
latest = json.loads(latest_path.read_text())
history = json.loads(history_path.read_text())

# {prefix}_{YYYYMMDD}_{HHMMSS}[_MS].jpg
FN = re.compile(r"^(?P<prefix>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_\d+)?\.jpg$")

def ts_from(name):
    m = FN.match(name or "")
    if not m: return None
    d, t = m.group("date"), m.group("time")
    return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:]}"

by_key = {(r.get("prefix"), r.get("name")): r for r in history}
appended = 0
for r in latest:
    if "image" not in r or "parsed" not in r:
        continue
    parsed = r.get("parsed") or {}
    if not parsed:
        continue
    prefix = r.get("prefix")
    name = r.get("image")
    key = (prefix, name)
    old = by_key.get(key)
    if old and old.get("source") == "label":
        continue  # never overwrite labels
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
    by_key[key] = row
    appended += 1

merged = sorted(by_key.values(), key=lambda r: r.get("ts") or "", reverse=True)
history_path.write_text(json.dumps(merged, indent=2) + "\n")
print(f"  Appended {appended} new rows; history now {len(merged)} total")
PY
fi

rm -f "$TMPLIST"
log "Done. $(jq 'length' "$LOCAL_OUT" 2>/dev/null || echo "?") predictions written."

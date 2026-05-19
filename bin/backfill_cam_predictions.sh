#!/usr/bin/env bash
# One-time backfill of per-image snow-depth predictions for snowhackers.com.
#
# Samples 4 frames/day per configured resort (00:00, 06:00, 12:00, 18:00 ±15 min),
# rsyncs them to the Blackwell, runs Run 7 inference, and merges results into
# cam_predictions_history.json. Idempotent — skips images that already have rows
# in the history file (unless --rerun is passed).
#
# Typical run: ~10K images, ~3 sec each on Blackwell → ~8 hours of GPU.
# Run in screen/tmux/run_in_background; it checkpoints every 50 predictions.
#
# Usage:
#   bin/backfill_cam_predictions.sh                # full backfill
#   bin/backfill_cam_predictions.sh --limit 50     # test with N images
#   bin/backfill_cam_predictions.sh --rerun        # re-predict everything

set -euo pipefail

BW_HOST="${BW_HOST:-192.168.0.117}"
BW_USER="${BW_USER:-trent-mahaffey}"
REMOTE_DIR="${REMOTE_DIR:-/home/$BW_USER/snowcam_predict}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LOCAL_IMG_DIR="/home/trent/snowcamtimelapse/out"
LOCAL_FT_DIR="/home/trent/snowcammeasurement/finetune"
LOCAL_CONFIG="$HERE/cam_config.json"
LOCAL_HISTORY="$HERE/cam_predictions_history.json"
ADAPTER="outputs/run_20260518_145633"

# Hours to sample per day; pick frames within ±MINUTES of these
SAMPLE_HOURS=(00 06 12 18)
SAMPLE_WINDOW_MIN=15

EXTRA_ARGS=""
while (( $# )); do
  case "$1" in
    --limit)  EXTRA_ARGS="$EXTRA_ARGS --limit $2"; shift 2 ;;
    --rerun)  EXTRA_ARGS="$EXTRA_ARGS --rerun"; shift ;;
    *) echo "unknown arg $1" >&2; exit 1 ;;
  esac
done

log() { echo "[$(date +%H:%M:%S)] $*"; }

# 1. Build manifest of sample images
log "Building sample manifest (4/day × resorts)"
MANIFEST=$(mktemp --suffix=.json)
python3 - "$LOCAL_CONFIG" "$LOCAL_IMG_DIR" "${SAMPLE_HOURS[*]}" "$SAMPLE_WINDOW_MIN" >"$MANIFEST" <<'PY'
import json, re, sys
from pathlib import Path

config_path, img_dir_s, hours_s, window_s = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
img_dir = Path(img_dir_s)
hours = set(h.strip().zfill(2) for h in hours_s.split())
window_min = int(window_s)

# {prefix}_{YYYYMMDD}_{HHMMSS}[_MS].jpg
FN = re.compile(r"^(?P<prefix>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_\d+)?\.jpg$")

config = json.loads(Path(config_path).read_text())
out = []
for entry in config:
    prefix = entry["prefix"]
    # Per (date, target_hour), pick the single closest frame
    closest = {}  # (date, hour) -> (delta_min, filename)
    for f in img_dir.glob(f"{prefix}_*.jpg"):
        m = FN.match(f.name)
        if not m:
            continue
        date = m.group("date")
        hh = int(m.group("time")[:2])
        mm = int(m.group("time")[2:4])
        for target in hours:
            th = int(target)
            delta = abs((hh * 60 + mm) - th * 60)
            if delta > window_min:
                continue
            key = (date, target)
            if key not in closest or delta < closest[key][0]:
                closest[key] = (delta, f.name)
    for (_d, _h), (_dt, fname) in closest.items():
        out.append({"prefix": prefix, "filename": fname})

# Stable order (so reruns sample the same frames)
out.sort(key=lambda r: (r["prefix"], r["filename"]))
print(json.dumps(out, indent=2))
PY

COUNT=$(jq 'length' "$MANIFEST")
log "  Manifest: $COUNT images"
if (( COUNT == 0 )); then
  log "Nothing to back-fill; bailing"
  exit 0
fi

# 2. Build the list of unique filenames for rsync
RSYNC_LIST=$(mktemp)
jq -r '.[].filename' "$MANIFEST" | sort -u > "$RSYNC_LIST"
RSYNC_COUNT=$(wc -l <"$RSYNC_LIST")
log "  Unique files to sync: $RSYNC_COUNT"

# 3. rsync images + scripts + config to Blackwell
log "Rsyncing images to Blackwell:$REMOTE_DIR/backfill_images/"
ssh "$BW_USER@$BW_HOST" "mkdir -p $REMOTE_DIR/backfill_images"
rsync -a --files-from="$RSYNC_LIST" "$LOCAL_IMG_DIR/" \
  "$BW_USER@$BW_HOST:$REMOTE_DIR/backfill_images/"

log "Rsyncing predict_history.py + cam_config.json + manifest"
rsync -a "$LOCAL_FT_DIR/predict_history.py" "$BW_USER@$BW_HOST:$REMOTE_DIR/"
rsync -a "$LOCAL_CONFIG"                    "$BW_USER@$BW_HOST:$REMOTE_DIR/cam_config.json"
rsync -a "$MANIFEST"                        "$BW_USER@$BW_HOST:$REMOTE_DIR/backfill_manifest.json"

# Seed remote history with existing local rows so labels and prior backfill
# work are preserved when --rerun isn't passed.
if [ -f "$LOCAL_HISTORY" ]; then
  log "Seeding remote with current cam_predictions_history.json"
  rsync -a "$LOCAL_HISTORY" \
    "$BW_USER@$BW_HOST:$REMOTE_DIR/cam_predictions_history.json"
fi

# 4. Run backfill on remote
log "Running predict_history.py on Blackwell (this is the long step)"
ssh "$BW_USER@$BW_HOST" bash <<EOSSH
set -euo pipefail
cd "$REMOTE_DIR"
ADAPTER_PATH=/home/$BW_USER/snow_finetune/$ADAPTER
if [ ! -d "\$ADAPTER_PATH" ]; then
  echo "Adapter not found at \$ADAPTER_PATH" >&2
  exit 1
fi
source /home/$BW_USER/snow_finetune/venv/bin/activate
python3 predict_history.py \\
  --adapter "\$ADAPTER_PATH" \\
  --config cam_config.json \\
  --manifest backfill_manifest.json \\
  --img-dir backfill_images \\
  --out cam_predictions_history.json \\
  $EXTRA_ARGS
EOSSH

# 5. Pull merged history back
log "Pulling cam_predictions_history.json to $LOCAL_HISTORY"
rsync -a "$BW_USER@$BW_HOST:$REMOTE_DIR/cam_predictions_history.json" "$LOCAL_HISTORY"

# 6. Re-merge labels from labels.json. predict_history.py loads its 'existing'
#    file once at startup, so label corrections made DURING the run don't take
#    effect. This step keeps every model row but replaces label rows with the
#    current state of snowcammeasurement/labels/labels.json.
log "Refreshing label rows from labels.json"
python3 "$HERE/bin/refresh_labels_in_history.py"

rm -f "$MANIFEST" "$RSYNC_LIST"

TOTAL=$(jq 'length' "$LOCAL_HISTORY")
LABELED=$(jq '[.[] | select(.source == "label")] | length' "$LOCAL_HISTORY")
MODELED=$(jq '[.[] | select(.source == "model")] | length' "$LOCAL_HISTORY")
log "Done. History: $TOTAL total ($LABELED labeled + $MODELED model)"

#!/usr/bin/env python3
"""Seed cam_predictions_history.json from snowcammeasurement/labels/labels.json.

Each labeled image becomes one row with source="label". Subsequent backfill +
hourly refresh will append source="model" rows for unlabeled frames.

Labeled rows are kept (not overwritten) because human labels are the source of
truth — the model is fit against them, so the model's prediction for a labeled
image is by construction worse or equal.
"""
import json
import re
import sys
from pathlib import Path

LABELS_PATH = Path("/home/trent/snowcammeasurement/labels/labels.json")
OUT_PATH = Path("/home/trent/snowhackers/cam_predictions_history.json")

# Filename pattern: {prefix}_{YYYYMMDD}_{HHMMSS}[_MS].jpg
# Snowmass adds an extra _NNN millisecond suffix; treat it as optional.
FILENAME_RE = re.compile(r"^(?P<prefix>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_\d+)?\.jpg$")


def prefix_from_name(name: str) -> str | None:
    m = FILENAME_RE.match(name)
    return m.group("prefix") if m else None


def main() -> int:
    data = json.loads(LABELS_PATH.read_text())
    rows = []
    skipped = {"no_filename": 0, "no_prefix": 0, "image_missing": 0, "no_depth": 0}

    for row in data:
        if row.get("status") == "image_missing":
            skipped["image_missing"] += 1
            continue
        name = row.get("name")
        if not name:
            skipped["no_filename"] += 1
            continue
        prefix = prefix_from_name(name)
        if not prefix:
            skipped["no_prefix"] += 1
            continue

        # Pick the primary depth value:
        #   depth_stake / indoor_display → depth_inches
        #   sherpa_bar → snowfall_24h_inches (fallback storm_inches)
        depth = row.get("depth_inches")
        stake_type = row.get("stake_type") or "depth_stake"
        if depth is None and stake_type == "sherpa_bar":
            depth = row.get("snowfall_24h_inches")
            if depth is None:
                depth = row.get("snowfall_storm_inches")
        if depth is None:
            skipped["no_depth"] += 1
            # Still emit a row but with depth_inches=None — useful for browsing
            # labeled-but-no-depth frames.
        rows.append({
            "prefix": prefix,
            "ts": row.get("ts"),
            "name": name,
            "depth_inches": depth,
            "confidence": row.get("confidence"),
            "source": "label",
            "stake_visible": row.get("stake_visible", True),
            "stake_type": stake_type,
            "label_id": row.get("id"),
        })

    # Sort by ts desc so the most recent labels appear first by default
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)

    OUT_PATH.write_text(json.dumps(rows, indent=2) + "\n")
    print(f"Wrote {len(rows)} rows to {OUT_PATH}")
    print(f"Skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

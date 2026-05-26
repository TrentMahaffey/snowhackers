#!/usr/bin/env python3
"""Refresh the label rows in cam_predictions_history.json from labels.json.

Use this after any labels.json correction sprint, or after the long backfill
completes (it caches an old snapshot of label rows at startup, so corrections
made during the run won't otherwise propagate).

Keeps all source="model" rows untouched; replaces every source="label" row with
a fresh version derived from labels.json.
"""
import json
import re
import sys
from pathlib import Path

LABELS_PATH  = Path("/home/trent/snowcammeasurement/labels/labels.json")
HISTORY_PATH = Path("/home/trent/snowhackers/cam_predictions_history.json")

FILENAME_RE = re.compile(r"^(?P<prefix>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_\d+)?\.jpg$")


def prefix_from(name):
    m = FILENAME_RE.match(name or "")
    return m.group("prefix") if m else None


def label_row(row):
    name = row.get("name")
    prefix = prefix_from(name)
    if not prefix:
        return None
    depth = row.get("depth_inches")
    stake_type = row.get("stake_type") or "depth_stake"
    if depth is None and stake_type == "sherpa_bar":
        depth = row.get("snowfall_24h_inches") or row.get("snowfall_storm_inches")
    return {
        "prefix": prefix,
        "ts": row.get("ts"),
        "name": name,
        "depth_inches": depth,
        "confidence": row.get("confidence"),
        "source": "label",
        "stake_visible": row.get("stake_visible", True),
        "stake_type": stake_type,
        "label_id": row.get("id"),
    }


def main():
    labels = json.loads(LABELS_PATH.read_text())
    history = json.loads(HISTORY_PATH.read_text())

    # Build a lookup: filename → model_* fields, so we can attach the model's
    # prior prediction onto each label row for side-by-side display.
    # We pull from BOTH the bare model rows (for newly-labeled images) AND from
    # the prior label rows themselves (which already had the carry-over baked in
    # — this matters because once a label is added, the model row is removed
    # from history, so subsequent refreshes would otherwise lose the data).
    model_by_key = {}
    for r in history:
        key = (r.get("prefix"), r.get("name"))
        if not key[0] or not key[1]:
            continue
        if r.get("source") == "model":
            model_by_key[key] = {
                "model_depth_inches": r.get("depth_inches"),
                "model_confidence": r.get("confidence"),
                "model_predicted_at": r.get("predicted_at"),
            }
        elif r.get("source") == "label" and r.get("model_depth_inches") is not None:
            model_by_key[key] = {
                "model_depth_inches": r.get("model_depth_inches"),
                "model_confidence": r.get("model_confidence"),
                "model_predicted_at": r.get("model_predicted_at"),
            }

    fresh_labels = []
    for row in labels:
        if row.get("status") == "image_missing":
            continue
        new = label_row(row)
        if not new:
            continue
        # Carry over the model's prior reading so the UI can show
        # "your reading" + "what the model thought" side by side.
        prior = model_by_key.get((new["prefix"], new["name"]))
        if prior is not None:
            new.update(prior)
        fresh_labels.append(new)
    label_keys = {(r["prefix"], r["name"]) for r in fresh_labels}

    # Keep all model rows that aren't shadowed by a fresh label
    kept_model = [r for r in history
                  if r.get("source") == "model"
                  and (r.get("prefix"), r.get("name")) not in label_keys]

    merged = fresh_labels + kept_model
    merged.sort(key=lambda r: r.get("ts") or "", reverse=True)

    HISTORY_PATH.write_text(json.dumps(merged, indent=2) + "\n")
    n_with_model = sum(1 for r in fresh_labels if r.get("model_depth_inches") is not None)
    print(f"Wrote {len(merged)} rows: {len(fresh_labels)} labels "
          f"({n_with_model} with prior model reading) + {len(kept_model)} model")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Bake the /snowcams/accuracy/ page: labeled stake images vs the production model's readings.

Truth comes from snowcammeasurement/labels:
  - accuracy_demo_truth_20260804.json   hand-checked samples at ~0/4/8/12" per resort
  - *_holdout_*.json                    held-out sets (never trained on) for newer / Sherpa cams
Every image is read by the adapter that cam actually uses in production (cams.json), so the page
shows what the site shows. Readings are cached in finetune/accuracy_demo_predictions.json; pass
--predict to re-run them on the Blackwell (a few minutes of GPU; skipped while training.lock exists).

    python3 bin/build_accuracy_demo.py --predict     # re-read images, then bake
    python3 bin/build_accuracy_demo.py               # re-bake from cached readings
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import camreg  # noqa: E402

SH = Path(__file__).resolve().parent.parent
SCM = Path("/home/trent/snowcammeasurement")
LABELS = SCM / "labels"
FT = SCM / "finetune"
CACHE = FT / "accuracy_demo_predictions.json"
OUT = SH / "snowdash/dashboard/data/accuracy_demo.json"
IMG_DIR = SH / "snowdash/dashboard/static/dashboard/accuracy_demo"
CAPTURES = Path("/home/trent/snowcamtimelapse/out")
BW = "trent-mahaffey@192.168.0.117"
BW_DIR = "/home/trent-mahaffey/snowcam_predict"
BW_FT = "/home/trent-mahaffey/snow_finetune"
# Frozen test-set readings per adapter (finetune/TRAINING_LOGS.MD)
TEST_PREDS = {"run_20260518_145633": "predictions_run7_test.json",
              "run_20261010_193642": "predictions_run9_test.json",
              "run_20261012_101416": "p10_test.json"}
BUCKETS = (0, 4, 8, 12)


def adapter_for(prefix):
    reg = camreg.load()
    cam = next((c for c in reg["cams"] if c["prefix"] == prefix), {})
    return cam.get("model", {}).get("adapter") or reg["default_adapter"]


def truth_rows():
    rows = []
    for t in json.loads((LABELS / "accuracy_demo_truth_20260804.json").read_text()):
        rows.append({"prefix": t["prefix"], "name": t["name"], "ts": t["ts"], "truth": t["truth"],
                     "bucket": t["bucket"], "set": "demo"})
    for f in sorted(LABELS.glob("*_holdout_*.json")):
        for r in json.loads(f.read_text()):
            sherpa = r.get("stake_type") == "sherpa_bar"
            truth = r.get("snowfall_24h_inches") if sherpa else r.get("depth_inches")
            if truth is None:
                continue
            name = r["name"]
            stamp = name[len(r["resort"]) + 1:][:15]  # YYYYMMDD_HHMMSS
            ts = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"
            rows.append({"prefix": r["resort"], "name": name, "ts": r.get("ts") or ts, "truth": float(truth),
                         "bucket": min(BUCKETS, key=lambda b: abs(b - truth)), "set": "holdout",
                         "host_path": r.get("host_path")})
    return rows


def predict(truth):
    """Read every truth image with its production adapter on the Blackwell; refresh CACHE."""
    if subprocess.run(["ssh", BW, f"test -f {BW_FT}/training.lock"]).returncode == 0:
        sys.exit("Blackwell is training (training.lock); try later or bake from cache")
    groups = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "img").mkdir()
        for t in truth:
            src = IMG_DIR / t["name"]
            if not src.exists():
                src = Path(t.get("host_path") or CAPTURES / t["name"])
            if not src.exists():
                print("  no image:", t["name"]); continue
            shutil.copy2(src, tmp / "img" / t["name"])
            groups.setdefault(adapter_for(t["prefix"]), []).append({"prefix": t["prefix"], "filename": t["name"]})
        subprocess.run(["python3", str(SH / "bin/camreg.py"), "export"], check=True, capture_output=True)
        shutil.copy2(SH / "cam_config.json", tmp / "cam_config.json")
        shutil.copy2(FT / "predict_history.py", tmp / "predict_history.py")
        for i, (adapter, items) in enumerate(groups.items()):
            (tmp / f"acc_manifest_g{i}.json").write_text(json.dumps(items))
        subprocess.run(["ssh", BW, f"rm -rf {BW_DIR}/acc && mkdir -p {BW_DIR}/acc"], check=True)
        subprocess.run(["rsync", "-a", f"{tmp}/", f"{BW}:{BW_DIR}/acc/"], check=True)
        cmds = [f"cd {BW_DIR}/acc", f"source {BW_FT}/venv/bin/activate"]
        for i, adapter in enumerate(groups):
            cmds.append(f"python3 predict_history.py --adapter {BW_FT}/{adapter} --config cam_config.json "
                        f"--manifest acc_manifest_g{i}.json --img-dir img --out acc_pred_g{i}.json --rerun")
        subprocess.run(["ssh", BW, " && ".join(cmds)], check=True)
        subprocess.run(["rsync", "-a", f"{BW}:{BW_DIR}/acc/acc_pred_g*.json", f"{tmp}/"], check=True)
        cache = {}
        for i, adapter in enumerate(groups):
            for r in json.loads((tmp / f"acc_pred_g{i}.json").read_text()):
                r["adapter"] = adapter.split("/")[-1]
                cache[r["name"]] = r
        CACHE.write_text(json.dumps(cache, indent=1) + "\n")
        print(f"read {len(cache)} images with {len(groups)} adapters -> {CACHE}")


def routed_test_stats():
    """Frozen test set scored with each image read by its cam's production adapter."""
    by_adapter = {a: {r["name"]: r for r in json.loads((FT / f).read_text())} for a, f in TEST_PREDS.items()}
    errs = []
    for name, r in by_adapter["run_20260518_145633"].items():
        a = adapter_for(r["resort"]).split("/")[-1]
        rr = by_adapter.get(a, by_adapter["run_20260518_145633"]).get(name, r)
        if rr.get("actual_primary") is not None and rr.get("predicted_primary") is not None:
            errs.append(abs(rr["predicted_primary"] - rr["actual_primary"]))
    n = len(errs)
    return {"n": n, "mae": round(sum(errs) / n, 1), "within1": round(sum(e <= 1 for e in errs) / n * 100),
            "within2": round(sum(e <= 2 for e in errs) / n * 100)}


def bake(truth):
    preds = json.loads(CACHE.read_text())
    names = camreg.names()
    train_imgs = set()
    for f in ("train.jsonl", "val.jsonl"):  # newest dataset (superset of older runs'): conservative
        for line in (FT / f).read_text().splitlines():
            r = json.loads(line)
            img = r.get("image") or r.get("image_path") or ""
            if img:
                train_imgs.add(Path(img).name)

    resorts = {}
    for t in truth:
        p = preds.get(t["name"])
        if not p:
            print("  no reading:", t["name"]); continue
        sherpa = p.get("stake_type") == "sherpa_bar"
        pred = p.get("snowfall_24h_inches") if sherpa else p.get("depth_inches")
        if pred is None:
            print("  unusable reading:", t["name"]); continue
        err = pred - t["truth"]; ae = abs(err)
        resorts.setdefault(t["prefix"], []).append({
            "bucket": t["bucket"], "truth": round(t["truth"], 1), "predicted": round(pred, 1),
            "error": round(err, 1),
            "err_class": "good" if ae <= 1 else ("warn" if ae <= 2 else "bad"),
            "err_label": ("%+.1f″" % err) if ae >= 0.05 else "exact",
            "confidence": p.get("confidence"), "name": t["name"], "ts": t["ts"],
            "in_training": t["set"] != "holdout" and t["name"] in train_imgs,
            "unit": "24-h total" if sherpa else "depth", "adapter": p.get("adapter"),
        })
        if not (IMG_DIR / t["name"]).exists() and t.get("host_path") and Path(t["host_path"]).exists():
            shutil.copy2(t["host_path"], IMG_DIR / t["name"])

    out_resorts, all_errs, points = [], [], []
    for prefix, samples in resorts.items():
        samples.sort(key=lambda s: (s["bucket"], s["truth"]))
        errs = [s["error"] for s in samples]; all_errs += errs
        aerrs = [abs(e) for e in errs]
        label = names.get(prefix, prefix)
        points += [{"t": s["truth"], "p": s["predicted"], "r": label, "h": not s["in_training"]} for s in samples]
        out_resorts.append({
            "prefix": prefix, "label": label, "n": len(samples),
            "mae": round(sum(aerrs) / len(aerrs), 1), "bias": round(sum(errs) / len(errs), 1),
            "within2": round(sum(1 for e in aerrs if e <= 2) / len(aerrs) * 100), "samples": samples,
        })
    out_resorts.sort(key=lambda r: r["label"])
    aerrs = [abs(e) for e in all_errs]; n = len(all_errs)
    adapters = sorted({s["adapter"] for r in out_resorts for s in r["samples"] if s.get("adapter")})
    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "model_run": "per-cam routing (" + ", ".join(adapters) + ")",
        "overall": {"n": n, "mae": round(sum(aerrs) / n, 1), "bias": round(sum(all_errs) / n, 1),
                    "within1": round(sum(1 for e in aerrs if e <= 1) / n * 100),
                    "within2": round(sum(1 for e in aerrs if e <= 2) / n * 100)},
        "test_stats": routed_test_stats(),
        "held_out_n": sum(1 for p in points if p["h"]),
        "resorts": out_resorts, "points": points,
    }
    OUT.write_text(json.dumps(data, indent=1))
    print("resorts:", len(out_resorts), "| overall:", data["overall"], "| test:", data["test_stats"],
          "| held-out:", data["held_out_n"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--predict", action="store_true", help="re-read all images on the Blackwell first")
    args = ap.parse_args()
    truth = truth_rows()
    if args.predict or not CACHE.exists():
        predict(truth)
    bake(truth)

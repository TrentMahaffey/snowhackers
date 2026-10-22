"""Snow cams: timelapse listing, latest readings, per-image search, accuracy demo."""

from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render

from .common import _SHOWCASE_DATA


def snowcams(request):
    """Snow cam timelapses viewer"""
    return render(request, "dashboard/snowcams.html")


def api_snowcam_videos(request):
    """API endpoint to list available timelapse videos"""
    # Timelapse directories with their durations and URL paths
    timelapse_dirs = {
        "daily": {"path": Path("/snowcam-timelapses"), "url_prefix": "/media/snowcams/", "label": "24 Hour"},
        "weekly": {"path": Path("/snowcam-timelapses7"), "url_prefix": "/media/snowcams7/", "label": "7 Day"},
        "monthly": {"path": Path("/snowcam-timelapses30"), "url_prefix": "/media/snowcams30/", "label": "30 Day"},
    }

    resorts = {name: prefix for prefix, name in _cam_names().items()}

    videos = []

    for duration_key, dir_info in timelapse_dirs.items():
        timelapses_dir = dir_info["path"]
        if not timelapses_dir.exists():
            continue

        for resort_name, pattern in resorts.items():
            # Find all videos for this resort
            resort_videos = sorted(
                timelapses_dir.glob(f"{pattern}_*.mp4"),
                reverse=True  # Newest first
            )

            for video_path in resort_videos[:7]:  # Last 7 entries
                filename = video_path.name
                # Extract date from filename (use END date if range, e.g., 20251031-20251130)
                try:
                    date_part = filename.split('_')[-1].split('.')[0]
                    if '-' in date_part and len(date_part) == 17:  # Date range like 20251031-20251130
                        date_str = date_part.split('-')[1][:8]  # Use end date
                    else:
                        date_str = date_part[:8]
                    date = datetime.strptime(date_str, "%Y%m%d")
                    date_display = date.strftime("%b %d, %Y")
                    date_sort = date.strftime("%Y-%m-%d")  # For filtering
                except:
                    date_display = "Unknown"
                    date_sort = "1970-01-01"

                videos.append({
                    "resort": resort_name,
                    "filename": filename,
                    "date": date_sort,  # YYYY-MM-DD for filtering
                    "date_display": date_display,  # Human-readable
                    "url": f"{dir_info['url_prefix']}{filename}",
                    "duration": duration_key,
                    "duration_label": dir_info["label"],
                })

    return JsonResponse(videos, safe=False)


def api_snowcam_predictions(request):
    """Latest model-generated snow-depth predictions per resort.

    Reads cam_predictions.json from the snowhackers root (mounted into the
    container). The file is refreshed hourly by bin/refresh_cam_predictions.sh
    which runs the fine-tuned Qwen2.5-VL model on the Blackwell.
    """
    from django.http import JsonResponse
    import json as _json
    # Look in a few candidate paths so this works in dev + container
    candidates = [
        Path("/snowcam-predictions/cam_predictions.json"),
        Path("/home/trent/snowhackers/cam_predictions.json"),
        Path(settings.BASE_DIR).parent / "cam_predictions.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = _json.loads(path.read_text())
                return JsonResponse({"predictions": data, "source": str(path)}, safe=False)
            except Exception as e:
                return JsonResponse({"error": f"failed to parse {path}: {e}"}, status=500)
    return JsonResponse({"predictions": [], "error": "cam_predictions.json not found"}, status=404)


# ---------------------------------------------------------------------------
# Image search (per-image predictions over the captured history)
# ---------------------------------------------------------------------------

# Filename pattern used to recognize the timestamp suffix on a snowcam image.
# Snowmass appends an extra _NNN millisecond field; treat it as optional.
SNOWCAM_FILENAME_RE = __import__("re").compile(
    r"^(?P<prefix>.+?)_(?P<date>\d{8})_(?P<time>\d{6})(?:_\d+)?\.jpg$"
)

_cams_cache = {"mtime": None, "names": {}}


def _cam_names():
    """{prefix: display name} from the cam registry (cams.json, mounted at /snowcam-config)."""
    import json as _json
    path = Path("/snowcam-config/cams.json")
    if not path.exists():
        path = Path(settings.BASE_DIR).parent / "cams.json"
    try:
        mtime = path.stat().st_mtime
        if _cams_cache["mtime"] != mtime:
            cams = _json.loads(path.read_text())["cams"]
            _cams_cache.update(mtime=mtime, names={c["prefix"]: c["name"] for c in cams})
    except (OSError, ValueError, KeyError):
        pass  # keep the last good copy
    return _cams_cache["names"]


_history_cache = {"path": None, "mtime": None, "rows": None}


def _load_predictions_history():
    """All per-image readings (model + label), newest first, from the cam_prediction table.

    Records are {prefix, ts, name, depth_inches, confidence, source, ...} (written by
    bin/history_db.py). Cached per worker until the table changes.
    """
    with connection.cursor() as cur:
        cur.execute("SELECT count(*), max(updated_at) FROM cam_prediction")
        key = cur.fetchone()
        if _history_cache["mtime"] != key:
            # One unsorted text blob + json.loads + a Python sort: ~0.4 s cold. Per-row json casts or
            # ORDER BY inside json_agg take 1.5-2 s (Postgres sorts the wide rows).
            cur.execute("SELECT coalesce(json_agg(rec), '[]')::text FROM cam_prediction")
            rows = __import__("json").loads(cur.fetchone()[0])
            rows.sort(key=lambda r: (r.get("ts") or "", r.get("name") or ""), reverse=True)
            _history_cache.update({"path": "postgres:cam_prediction", "mtime": key, "rows": rows})
    return _history_cache["rows"], _history_cache["path"]


def snowcam_search(request):
    """Render the advanced image-search page (filters + pagination)."""
    return render(request, "dashboard/snowcam_search.html")


def snowcam_predictions_page(request):
    """Render the simple per-image predictions feed (image + model reading)."""
    return render(request, "dashboard/snowcam_predictions.html")


def api_snowcam_resorts(request):
    """List of resorts that have at least one row in the predictions history."""
    rows, _ = _load_predictions_history()
    seen = set()
    out = []
    for r in rows:
        prefix = r.get("prefix")
        if prefix and prefix not in seen:
            seen.add(prefix)
            out.append({
                "prefix": prefix,
                "label": _cam_names().get(prefix, prefix),
            })
    out.sort(key=lambda x: x["label"])
    return JsonResponse({"resorts": out, "total": len(out)})


def api_snowcam_images(request):
    """Paginated image search over the predictions history.

    Query params:
      resort:      comma-separated list of filename prefixes (e.g. "alta_snowstake,park_city")
      start_date:  YYYY-MM-DD (inclusive)
      end_date:    YYYY-MM-DD (inclusive)
      min_inches:  float; rows where depth_inches < min are dropped (rows with no
                   depth are dropped UNLESS the inches filter is fully absent)
      max_inches:  float; rows where depth_inches > max are dropped
      page:        1-based page number (default 1)
      per_page:    items per page (default 60, max 200)
      sort:        "ts_desc" (default), "ts_asc", "depth_desc", "depth_asc"
    """

    rows, source = _load_predictions_history()

    # --- parse filters ---
    resort_raw = request.GET.get("resort", "").strip()
    resort_filter = {r for r in (s.strip() for s in resort_raw.split(",")) if r}

    start_date = request.GET.get("start_date", "").strip()
    end_date = request.GET.get("end_date", "").strip()
    start_ts = f"{start_date} 00:00:00" if start_date else None
    end_ts = f"{end_date} 23:59:59" if end_date else None

    def _parse_float(name):
        raw = request.GET.get(name, "").strip()
        if raw == "":
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    min_inches = _parse_float("min_inches")
    max_inches = _parse_float("max_inches")
    inches_filter_active = (min_inches is not None) or (max_inches is not None)

    # Separate filter on the MODEL's predicted depth (regardless of whether the
    # row is source=label or source=model). For label rows, looks at
    # model_depth_inches; for model rows, looks at depth_inches.
    model_min = _parse_float("model_min_inches")
    model_max = _parse_float("model_max_inches")
    model_filter_active = (model_min is not None) or (model_max is not None)

    # Source filter: "all" (default), "label", "model"
    source_filter = request.GET.get("source", "all").strip() or "all"
    # Confidence filter: "all" (default), "high", "medium", "low"
    conf_filter = request.GET.get("confidence", "all").strip() or "all"
    # disagree filter: when "1", only show images where label != model prediction
    only_disagreements = request.GET.get("disagree", "").strip() in {"1", "true"}

    try:
        page = max(1, int(request.GET.get("page", "1") or "1"))
    except ValueError:
        page = 1
    try:
        per_page = max(1, min(200, int(request.GET.get("per_page", "60") or "60")))
    except ValueError:
        per_page = 60

    sort = request.GET.get("sort", "ts_desc")

    # --- filter ---
    filtered = []
    for r in rows:
        if resort_filter and r.get("prefix") not in resort_filter:
            continue
        ts = r.get("ts") or ""
        if start_ts and ts < start_ts:
            continue
        if end_ts and ts > end_ts:
            continue
        depth = r.get("depth_inches")
        if inches_filter_active:
            if depth is None:
                continue
            if min_inches is not None and depth < min_inches:
                continue
            if max_inches is not None and depth > max_inches:
                continue
        if source_filter != "all" and r.get("source") != source_filter:
            continue
        if conf_filter != "all" and r.get("confidence") != conf_filter:
            continue
        if model_filter_active:
            # Extract the model's reading: depth_inches for model rows, or
            # model_depth_inches that was carried over onto a label row.
            md = (r.get("model_depth_inches")
                  if r.get("source") == "label"
                  else r.get("depth_inches"))
            if md is None:
                continue
            if model_min is not None and md < model_min:
                continue
            if model_max is not None and md > model_max:
                continue
        if only_disagreements:
            # Only rows that have both label and model readings, where they differ.
            md = r.get("model_depth_inches")
            if r.get("source") != "label" or md is None or depth is None:
                continue
            if abs((md or 0) - (depth or 0)) < 0.5:
                continue
        filtered.append(r)

    # --- sort ---
    if sort == "ts_asc":
        filtered.sort(key=lambda r: r.get("ts") or "")
    elif sort == "depth_desc":
        filtered.sort(key=lambda r: (-(r.get("depth_inches") or -1), r.get("ts") or ""), reverse=False)
    elif sort == "depth_asc":
        filtered.sort(key=lambda r: ((r.get("depth_inches") if r.get("depth_inches") is not None else 1e9), r.get("ts") or ""))
    else:  # ts_desc default
        filtered.sort(key=lambda r: r.get("ts") or "", reverse=True)

    total = len(filtered)
    start = (page - 1) * per_page
    end = start + per_page
    page_rows = filtered[start:end]

    # --- shape response ---
    items = []
    for r in page_rows:
        prefix = r.get("prefix") or ""
        filename = r.get("name") or ""
        items.append({
            "resort_prefix": prefix,
            "resort_label": _cam_names().get(prefix, prefix),
            "ts": r.get("ts"),
            "filename": filename,
            "image_url": f"/media/snapshots/{filename}" if filename else None,
            "depth_inches": r.get("depth_inches"),
            "confidence": r.get("confidence"),
            "source": r.get("source"),  # "label" | "model" | "manual"
            "label_id": r.get("label_id"),  # numeric id from labels.json (label rows only)
            # Side-by-side: when source=="label", expose the model's prior reading
            # (set by refresh_labels_in_history.py). When source=="model" these
            # fields are absent — the model reading IS depth_inches.
            "model_depth_inches": r.get("model_depth_inches"),
            "model_confidence": r.get("model_confidence"),
        })

    return JsonResponse({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
        "source_file": source,
    })


def api_snowcam_depth_grid(request):
    """For each resort, find the prediction closest to each target depth.

    Targets default to 3", 6", 9", 12". For each resort × target we pick the
    single closest match within tolerance. Model predictions (source=model)
    are preferred; if no model prediction exists within tolerance for a given
    target, we fall back to hand-labeled training images (source=label) so
    newly-onboarded cams without seasonal predictions still have something to
    show.

    Query params:
      targets:   comma-separated list of target inches (default "3,6,9,12")
      tolerance: max distance from target in inches (default 1.5)
    """
    from django.http import JsonResponse

    targets_raw = request.GET.get("targets", "3,6,9,12")
    try:
        targets = [float(x) for x in targets_raw.split(",") if x.strip()]
    except ValueError:
        targets = [3.0, 6.0, 9.0, 12.0]
    try:
        tolerance = float(request.GET.get("tolerance", "1.5"))
    except ValueError:
        tolerance = 1.5

    rows, source = _load_predictions_history()

    # Group rows by resort prefix, split into model and label buckets
    model_by_resort = {}
    label_by_resort = {}
    for r in rows:
        depth = r.get("depth_inches")
        if depth is None:
            continue
        prefix = r.get("prefix")
        if not prefix:
            continue
        src = r.get("source")
        if src == "model":
            model_by_resort.setdefault(prefix, []).append(r)
        elif src == "label":
            label_by_resort.setdefault(prefix, []).append(r)

    def _closest(rows_for_resort, target):
        best = None
        best_dist = tolerance
        for r in rows_for_resort:
            dist = abs(r["depth_inches"] - target)
            if dist <= best_dist:
                best = r
                best_dist = dist
        return best

    all_prefixes = set(model_by_resort) | set(label_by_resort)
    out_resorts = []
    for prefix in all_prefixes:
        model_rows = model_by_resort.get(prefix, [])
        label_rows = label_by_resort.get(prefix, [])
        samples = []
        for target in targets:
            best = _closest(model_rows, target)
            used_source = "model"
            if best is None:
                best = _closest(label_rows, target)
                used_source = "label"
            if best is None:
                samples.append({"target": target, "match": None})
            else:
                name = best.get("name") or ""
                samples.append({
                    "target": target,
                    "match": {
                        "filename": name,
                        "image_url": f"/media/snapshots/{name}" if name else None,
                        "ts": best.get("ts"),
                        "depth_inches": best.get("depth_inches"),
                        "confidence": best.get("confidence"),
                        "source": used_source,
                    },
                })
        out_resorts.append({
            "prefix": prefix,
            "label": _cam_names().get(prefix, prefix),
            "samples": samples,
            "total_predictions": len(model_rows),
            "total_labels": len(label_rows),
        })

    out_resorts.sort(key=lambda x: x["label"])

    return JsonResponse({
        "targets": targets,
        "tolerance": tolerance,
        "resorts": out_resorts,
        "source_file": source,
    })


def snowcam_accuracy_page(request):
    """Accuracy demo: hand-labeled stake images vs model readings, per resort and depth.

    Data is baked offline into dashboard/data/accuracy_demo.json by the
    accuracy-demo eval (see snowhackers bin/ + snowcammeasurement/finetune).
    """
    import json as _json
    path = _SHOWCASE_DATA / "accuracy_demo.json"
    try:
        data = _json.loads(path.read_text())
    except Exception:
        data = {}
    return render(request, "dashboard/snowcam_accuracy.html", {
        "overall": data.get("overall", {}),
        "test_stats": data.get("test_stats", {}),
        "resorts": data.get("resorts", []),
        "points_json": data.get("points", []),
        "model_run": data.get("model_run", ""),
        "generated_at": data.get("generated_at", ""),
    })

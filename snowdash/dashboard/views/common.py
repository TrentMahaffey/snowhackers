"""Helpers shared by the dashboard view modules."""

from pathlib import Path



# Baked JSON the site reads (highlights, forecast, accuracy demo, curation overrides)
_SHOWCASE_DATA = Path(__file__).resolve().parent.parent / "data"


def _read_json(path, default):
    import json as _json
    try:
        return _json.loads(Path(path).read_text())
    except Exception:
        return default

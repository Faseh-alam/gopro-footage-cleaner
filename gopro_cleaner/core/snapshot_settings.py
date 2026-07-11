"""Snapshot spacing config — tune garbage tolerance per client/dataset.

Edit ``gopro_cleaner/snapshot_settings.json`` (or set env overrides) without
changing code. Defaults match the cleaning tolerance model:

  tolerance = duration * garbage_percent
  interval  = clamp(tolerance / N, MIN_INTERVAL, MAX_INTERVAL)
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULTS = {
    "garbage_percent": 0.10,
    "resolution_factor": 3,
    "min_interval_sec": 15.0,
    "max_interval_sec": 90.0,
    "snapshot_width": 160,
    "jpeg_quality": 4,
    "label_preview_count": 8,
    "label_preview_interval_sec": 5.0,
    "label_preview_span_sec": 40.0,
}

_SETTINGS_FILE = Path(__file__).resolve().parent / "snapshot_settings.json"


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@lru_cache(maxsize=1)
def load_snapshot_settings() -> dict:
    data = dict(DEFAULTS)
    if _SETTINGS_FILE.exists():
        try:
            loaded = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update({k: loaded[k] for k in DEFAULTS if k in loaded})
        except (json.JSONDecodeError, OSError):
            pass

    overrides = {
        "garbage_percent": _env_float("GOPRO_GARBAGE_PERCENT"),
        "resolution_factor": _env_int("GOPRO_RESOLUTION_FACTOR"),
        "min_interval_sec": _env_float("GOPRO_MIN_INTERVAL"),
        "max_interval_sec": _env_float("GOPRO_MAX_INTERVAL"),
        "snapshot_width": _env_int("GOPRO_SNAPSHOT_WIDTH"),
        "jpeg_quality": _env_int("GOPRO_JPEG_QUALITY"),
    }
    for key, value in overrides.items():
        if value is not None:
            data[key] = value

    data["garbage_percent"] = float(data["garbage_percent"])
    data["resolution_factor"] = max(1, int(data["resolution_factor"]))
    data["min_interval_sec"] = float(data["min_interval_sec"])
    data["max_interval_sec"] = float(data["max_interval_sec"])
    if data["max_interval_sec"] < data["min_interval_sec"]:
        data["max_interval_sec"] = data["min_interval_sec"]
    data["snapshot_width"] = max(32, int(data["snapshot_width"]))
    data["jpeg_quality"] = max(2, min(31, int(data["jpeg_quality"])))
    return data


def reload_snapshot_settings() -> dict:
    load_snapshot_settings.cache_clear()
    return load_snapshot_settings()

"""Paths and persistent config for the SD offloader."""

from __future__ import annotations

import json
import threading
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = APP_ROOT / "config.json"
STATE_DIR = APP_ROOT / "state"
BATCHES_SUBDIR = "Batches"
PROGRESS_FILENAME = ".gopro_offload_progress.json"

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "s3_uri": "",
    "ssd1": "",
    "ssd2": "",
    "last_batch": "",
    "mode": "ssd_only",  # ssd_only | ssd_and_aws
    "port": 8877,
}


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG.copy())
        return DEFAULT_CONFIG.copy()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    merged = DEFAULT_CONFIG.copy()
    merged.update(data)
    for key, value in DEFAULT_CONFIG.items():
        merged.setdefault(key, value)
    return merged


def save_config(data: dict) -> dict:
    ensure_dirs()
    current = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            current.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    current.update(data)
    with _lock:
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current

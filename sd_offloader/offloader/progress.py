"""Resume progress files on SD cards and local state mirrors."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import PROGRESS_FILENAME, STATE_DIR, ensure_dirs

_lock = threading.Lock()


def progress_path(card_root: Path) -> Path:
    return card_root / PROGRESS_FILENAME


def load_progress(card_root: Path) -> dict | None:
    path = progress_path(card_root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_progress(card_root: Path, data: dict) -> None:
    path = progress_path(card_root)
    payload = json.dumps(data, indent=2)
    # Write atomically when possible
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            path.write_text(payload, encoding="utf-8")
        except OSError:
            pass
    _mirror_local(data)


def mark_file_done(card_root: Path, data: dict, rel: str, size: int) -> dict:
    files = data.setdefault("files", {})
    files[rel] = {"size": size, "status": "done"}
    save_progress(card_root, data)
    return data


def is_file_done(data: dict | None, rel: str, size: int, dest_file: Path) -> bool:
    if not data:
        return False
    entry = (data.get("files") or {}).get(rel)
    if not entry or entry.get("status") != "done":
        return False
    if int(entry.get("size") or 0) != int(size):
        return False
    try:
        return dest_file.exists() and dest_file.stat().st_size == size
    except OSError:
        return False


def _mirror_local(data: dict) -> None:
    ensure_dirs()
    card_id = str(data.get("card_id") or "unknown")
    batch = str(data.get("batch") or "batch").replace("/", "_")
    path = STATE_DIR / f"{batch}__{card_id}.json"
    with _lock:
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass


def clear_progress(card_root: Path) -> None:
    path = progress_path(card_root)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass

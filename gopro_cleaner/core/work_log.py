"""Persist egress work-time sessions for clean/label throughput tracking."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORK_LOG_FILE = PROJECT_ROOT / "work_sessions.json"

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load() -> list[dict]:
    if not WORK_LOG_FILE.exists():
        return []
    try:
        data = json.loads(WORK_LOG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _save(rows: list[dict]) -> None:
    WORK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORK_LOG_FILE.write_text(json.dumps(rows, indent=2), encoding="utf-8")


def append_work_session(payload: dict) -> dict:
    root = str(payload.get("root", "")).strip()
    if not root:
        raise ValueError("root is required")

    clean_seconds = max(0, int(float(payload.get("clean_seconds", 0) or 0)))
    label_seconds = max(0, int(float(payload.get("label_seconds", 0) or 0)))
    total_seconds = clean_seconds + label_seconds
    event = str(payload.get("event", "checkpoint")).strip() or "checkpoint"
    files_total = int(payload.get("files_total", 0) or 0)
    files_done = int(payload.get("files_done", 0) or 0)

    row = {
        "saved_at": _now_iso(),
        "root": root,
        "event": event,
        "clean_seconds": clean_seconds,
        "label_seconds": label_seconds,
        "total_seconds": total_seconds,
        "clean_hms": _hms(clean_seconds),
        "label_hms": _hms(label_seconds),
        "total_hms": _hms(total_seconds),
        "files_total": files_total,
        "files_done": files_done,
        "phase": str(payload.get("phase", "")).strip(),
    }

    with _lock:
        rows = _load()
        rows.append(row)
        _save(rows[-500:])
    return row


def list_work_sessions(limit: int = 50) -> list[dict]:
    with _lock:
        rows = _load()
    return list(reversed(rows[-max(1, min(limit, 200)) :]))


def _hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

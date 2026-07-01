"""Persistent task tag list for Eager Review."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS_FILE = PROJECT_ROOT / "eager_tasks.json"

DEFAULT_TASKS = [
    "cardboard folding",
    "picking",
    "sorting",
    "assembly",
    "inspection",
    "walking / transit",
    "setup / calibration",
    "task-stitching",
]

_lock = threading.RLock()


def _tasks_path() -> Path:
    custom = Path(os.environ.get("EAGER_TASKS_FILE", str(DEFAULT_TASKS_FILE)))
    return custom.expanduser()


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name.strip().lower())
    return re.sub(r"[-\s]+", "-", slug).strip("-") or "task"


def _write_tasks_unlocked(path: Path, tasks: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        task = task.strip()
        if not task:
            continue
        key = task.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tasks": cleaned}, indent=2), encoding="utf-8")
    return cleaned


def _read_tasks_unlocked(path: Path) -> list[str]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tasks = [str(item).strip() for item in data.get("tasks", []) if str(item).strip()]
            if tasks:
                return tasks
        except (json.JSONDecodeError, OSError):
            pass
    return list(DEFAULT_TASKS)


def load_tasks() -> list[str]:
    path = _tasks_path()
    with _lock:
        tasks = _read_tasks_unlocked(path)
        if not path.exists():
            return _write_tasks_unlocked(path, tasks)
        return tasks


def save_tasks(tasks: list[str]) -> list[str]:
    path = _tasks_path()
    with _lock:
        return _write_tasks_unlocked(path, tasks)


def add_task(name: str) -> list[str]:
    name = name.strip()
    if not name:
        raise ValueError("Task name cannot be empty")
    with _lock:
        path = _tasks_path()
        tasks = _read_tasks_unlocked(path)
        if any(existing.lower() == name.lower() for existing in tasks):
            return tasks
        tasks.append(name)
        return _write_tasks_unlocked(path, tasks)


def task_folder_name(task: str) -> str:
    return _slug(task)

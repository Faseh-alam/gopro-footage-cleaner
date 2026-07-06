"""Persistent task tag list for Eager Review."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS_FILE = PROJECT_ROOT / "eager_tasks.json"
BUNDLED_TASKS_FILE = PROJECT_ROOT / "eager_tasks.default.json"
TASK_LIST_VERSION = 2

_lock = threading.RLock()


def _tasks_path() -> Path:
    custom = Path(os.environ.get("EAGER_TASKS_FILE", str(DEFAULT_TASKS_FILE)))
    return custom.expanduser()


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name.strip().lower())
    return re.sub(r"[-\s]+", "-", slug).strip("-") or "task"


def _clean_task_names(tasks: list[str]) -> list[str]:
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
    return cleaned


def bundled_tasks() -> list[str]:
    """Canonical task list shipped with the app (same on Mac and Windows)."""
    if BUNDLED_TASKS_FILE.exists():
        try:
            data = json.loads(BUNDLED_TASKS_FILE.read_text(encoding="utf-8"))
            tasks = _clean_task_names([str(item) for item in data.get("tasks", [])])
            if tasks:
                return tasks
        except (json.JSONDecodeError, OSError):
            pass
    return _clean_task_names(
        [
            "Fabric-Cutting-Scissor",
            "Fabric-Cutting-Machine",
            "Fabric-Layering",
            "Fabric Loading",
            "Garment-Stitching-Overlock",
            "Garment-Stitching-Joint-Seam",
            "Garment-Label-Attachment",
            "Garment-Loop-Attachment",
            "Binding-Pre-Fold-Stitching",
            "Garment-Zip-Attachment",
            "Garment-Back-Panel-Attachment",
            "Garment-Edge-Hemming",
            "Garment-Bartacking",
            "Zip-Tape-Cutting",
            "Zip-Tape-Bartacking",
            "Loop-Tape-Preparation",
            "Garment-Button-Attachment",
            "Garment-Stitching-General",
            "Garment-Quality-Checking",
            "Garment-Inside-Out",
            "Garment-Iron-Press",
            "Garment-Packing-General",
            "Garment-Folding-General",
            "Garment-Folding-Cardboard-Insert",
            "Garment-Pair-Folding",
            "Garment-Tag-Attachment",
            "Garment-Belly-Band-Wrapping",
            "Belly-Band-Assembly",
            "Cardboard Assembly",
            "Garment-Safety-Sticker",
            "Garment-Carton-Packing",
            "Bobbin-Changeover",
            "Quilting-Machine-Operation",
        ]
    )


def _write_tasks_unlocked(path: Path, tasks: list[str]) -> list[str]:
    cleaned = _clean_task_names(tasks)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": TASK_LIST_VERSION, "tasks": cleaned}, indent=2),
        encoding="utf-8",
    )
    return cleaned


def _read_tasks_unlocked(path: Path) -> list[str]:
    canonical = bundled_tasks()
    if not path.exists():
        return canonical

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return canonical

    if data.get("version") != TASK_LIST_VERSION:
        return canonical

    tasks = _clean_task_names([str(item) for item in data.get("tasks", [])])
    return tasks if tasks else canonical


def load_tasks() -> list[str]:
    path = _tasks_path()
    with _lock:
        tasks = _read_tasks_unlocked(path)
        if not path.exists() or _file_needs_refresh(path):
            return _write_tasks_unlocked(path, tasks)
        return tasks


def _file_needs_refresh(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    if data.get("version") != TASK_LIST_VERSION:
        return True
    return not data.get("tasks")


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

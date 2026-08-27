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
_profile = "default"  # "default" | "scaleai"


SCALEAI_TASKS_FILE = PROJECT_ROOT / "scaleai_tasks.json"


def get_profile() -> str:
    with _lock:
        return _profile


def set_profile(name: str) -> str:
    """Switch between normal textile tasks and empty ScaleAI micro-task list."""
    global _profile
    key = (name or "default").strip().lower()
    if key not in {"default", "scaleai"}:
        raise ValueError("profile must be 'default' or 'scaleai'")
    with _lock:
        _profile = key
        return _profile


def _tasks_path() -> Path:
    custom = os.environ.get("EAGER_TASKS_FILE", "").strip()
    if custom:
        return Path(custom).expanduser()
    with _lock:
        if _profile == "scaleai":
            return SCALEAI_TASKS_FILE
    return DEFAULT_TASKS_FILE


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
    payload = {"version": TASK_LIST_VERSION, "tasks": cleaned}
    if path == SCALEAI_TASKS_FILE or _profile == "scaleai":
        payload["profile"] = "scaleai"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return cleaned


def _read_tasks_unlocked(path: Path) -> list[str]:
    """Load tasks from disk.

    ScaleAI profile is allowed to be empty (operators add micro-tasks live).
    Default profile falls back to the bundled textile list when missing/empty.
    """
    allow_empty = _profile == "scaleai" or path == SCALEAI_TASKS_FILE
    if not path.exists():
        if allow_empty:
            return []
        return bundled_tasks()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [] if allow_empty else bundled_tasks()

    if data.get("version") != TASK_LIST_VERSION:
        return [] if allow_empty else bundled_tasks()

    tasks = _clean_task_names([str(item) for item in data.get("tasks", [])])
    if tasks:
        return tasks
    return [] if allow_empty else bundled_tasks()


def load_tasks() -> list[str]:
    path = _tasks_path()
    with _lock:
        tasks = _read_tasks_unlocked(path)
        if not path.exists():
            return _write_tasks_unlocked(path, tasks)
        if _file_needs_refresh(path) and _profile != "scaleai":
            return _write_tasks_unlocked(path, tasks)
        return tasks


def _file_needs_refresh(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True
    if data.get("version") != TASK_LIST_VERSION:
        return True
    # Empty is valid for ScaleAI — do not treat it as "needs refresh".
    if _profile == "scaleai" or path == SCALEAI_TASKS_FILE:
        return False
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


def is_default_task(name: str) -> bool:
    """Bundled textile defaults are protected only in the default profile."""
    if _profile == "scaleai":
        return False
    key = name.strip().lower()
    if not key:
        return False
    return any(existing.lower() == key for existing in bundled_tasks())


def remove_task(name: str) -> list[str]:
    name = name.strip()
    if not name:
        raise ValueError("Task name cannot be empty")
    if is_default_task(name):
        raise ValueError("Default tasks cannot be removed")
    with _lock:
        path = _tasks_path()
        tasks = _read_tasks_unlocked(path)
        next_tasks = [t for t in tasks if t.lower() != name.lower()]
        if len(next_tasks) == len(tasks):
            raise ValueError("Task not found")
        return _write_tasks_unlocked(path, next_tasks)


def task_folder_name(task: str) -> str:
    """Folder name for a task — keeps display casing, strips path-illegal chars."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", task.strip())
    cleaned = re.sub(r"\s+", "-", cleaned).strip(" .-")
    return cleaned or "task"

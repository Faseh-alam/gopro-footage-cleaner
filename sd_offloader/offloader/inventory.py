"""Discover transferable footage under DCIM/xxxGOPRO.

Copies:
  - MP4s inside task folders (legacy labeled layout)
  - MP4s directly in the GOPRO folder (mapped wholes)
  - Matching ``*.segments.json`` / ``*.segments.txt`` sidecars
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .detect import _find_gopro_root

SKIP_NAMES = {
    ".trash",
    ".trashes",
    "system volume information",
    "$recycle.bin",
    ".spotlight-v100",
    ".fseventsd",
}

SIDECAR_SUFFIXES = (".segments.json", ".segments.txt")


def _task_slugs_from_cleaner() -> set[str]:
    candidates = [
        Path(__file__).resolve().parents[2] / "eager_tasks.default.json",
        Path(__file__).resolve().parents[2] / "eager_tasks.json",
    ]
    slugs: set[str] = set()
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for name in data.get("tasks", []):
                slug = _slug(str(name))
                if slug:
                    slugs.add(slug)
        except (json.JSONDecodeError, OSError):
            continue
    return slugs


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name.strip().lower())
    return re.sub(r"[-\s]+", "-", slug).strip("-")


KNOWN_TASK_SLUGS = _task_slugs_from_cleaner()


def _file_entry(rel: str, item: Path, task: str) -> dict | None:
    try:
        size = item.stat().st_size
    except OSError:
        return None
    return {
        "rel": rel,
        "source": str(item.resolve()),
        "size": size,
        "task": task,
    }


def _add_sidecars(files: list[dict], mp4: Path, rel_mp4: str, task: str) -> None:
    for suffix in SIDECAR_SUFFIXES:
        side = mp4.with_name(f"{mp4.stem}{suffix}")
        if not side.is_file() or side.name.startswith("._"):
            continue
        rel = str(Path(rel_mp4).with_name(side.name)).replace("\\", "/")
        entry = _file_entry(rel, side, task)
        if entry:
            files.append(entry)


def list_transfer_files(card_root: Path) -> list[dict]:
    """Return files to copy relative to GOPRO root."""
    gopro = _find_gopro_root(card_root)
    if gopro is None:
        return []

    files: list[dict] = []
    seen: set[str] = set()

    def add(entry: dict | None) -> None:
        if not entry:
            return
        if entry["rel"] in seen:
            return
        seen.add(entry["rel"])
        files.append(entry)

    try:
        children = list(gopro.iterdir())
    except OSError:
        return []

    # Root-level MP4s (timestamp-mapped wholes stay in GOPRO until server trim).
    for item in sorted(children, key=lambda p: p.name.lower()):
        if not item.is_file() or item.name.startswith("._"):
            continue
        if item.suffix.upper() != ".MP4":
            continue
        rel = item.name
        add(_file_entry(rel, item, ""))
        _add_sidecars(files, item, rel, "")

    # Task folders (legacy labeled layout + any nested wholes).
    for task_dir in sorted([p for p in children if p.is_dir()], key=lambda p: p.name.lower()):
        name_lower = task_dir.name.lower()
        if name_lower in SKIP_NAMES or name_lower.startswith("."):
            continue
        try:
            task_children = list(task_dir.iterdir())
        except OSError:
            continue

        for item in task_children:
            if not item.is_file() or item.name.startswith("._"):
                continue
            if item.suffix.upper() != ".MP4":
                continue
            rel = f"{task_dir.name}/{item.name}"
            add(_file_entry(rel, item, task_dir.name))
            _add_sidecars(files, item, rel, task_dir.name)

    return files


def total_bytes(files: list[dict]) -> int:
    return sum(int(f.get("size") or 0) for f in files)

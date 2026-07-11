"""Discover transferable task folders under DCIM/xxxGOPRO."""

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


def list_transfer_files(card_root: Path) -> list[dict]:
    """Return files to copy relative to GOPRO root as task/file.MP4."""
    gopro = _find_gopro_root(card_root)
    if gopro is None:
        return []

    files: list[dict] = []
    try:
        task_dirs = [p for p in gopro.iterdir() if p.is_dir()]
    except OSError:
        return []

    for task_dir in sorted(task_dirs, key=lambda p: p.name.lower()):
        name_lower = task_dir.name.lower()
        if name_lower in SKIP_NAMES or name_lower.startswith("."):
            continue
        try:
            children = list(task_dir.iterdir())
        except OSError:
            continue

        for item in children:
            if not item.is_file():
                continue
            if item.name.startswith("._"):
                continue
            if item.suffix.upper() != ".MP4":
                continue
            rel = f"{task_dir.name}/{item.name}"
            try:
                size = item.stat().st_size
            except OSError:
                continue
            files.append(
                {
                    "rel": rel,
                    "source": str(item.resolve()),
                    "size": size,
                    "task": task_dir.name,
                }
            )

    return files


def total_bytes(files: list[dict]) -> int:
    return sum(int(f.get("size") or 0) for f in files)

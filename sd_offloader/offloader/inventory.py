"""Discover transferable MP4s + segment sidecars under DCIM/xxxGOPRO."""

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
    """Return files to copy relative to the GOPRO root.

    Primary layout (label-only workflow): raw ``*.MP4`` files directly under
    ``DCIM/###GOPRO`` together with their ``*.segments.json`` sidecars written
    by GoPro Cleaner. Legacy task folders (pre-trimmed clips) are still picked
    up when present.
    """
    gopro = _find_gopro_root(card_root)
    if gopro is None:
        return []

    files: list[dict] = []
    try:
        entries = list(gopro.iterdir())
    except OSError:
        return []
    task_dirs = [p for p in entries if p.is_dir()]
    root_files = [p for p in entries if p.is_file() and not p.name.startswith("._")]

    # 1) Raw labeled videos + sidecars at the GOPRO root → batch folder.
    for item in sorted(root_files, key=lambda p: p.name.lower()):
        is_mp4 = item.suffix.upper() == ".MP4"
        is_sidecar = item.name.lower().endswith(".segments.json")
        if not is_mp4 and not is_sidecar:
            continue
        try:
            size = item.stat().st_size
        except OSError:
            continue
        row = {
            "rel": item.name,
            "source": str(item.resolve()),
            "size": size,
            "task": "",
        }
        if is_mp4:
            sidecar = item.with_name(f"{item.stem}.segments.json")
            if sidecar.is_file():
                # Segments get embedded into the SSD copy of this MP4.
                row["embed_json"] = str(sidecar.resolve())
        files.append(row)

    # 2) Legacy pre-trimmed task folders.
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

"""Discover transferable MP4s + JSON sidecars under DCIM/xxxGOPRO."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .detect import find_gopro_dirs, is_json_sidecar

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


def _embed_sidecar_for(mp4: Path) -> Path | None:
    """Prefer Cleaner ``.segments.json``, else plain ``.JSON`` / ``.json``."""
    preferred = [
        mp4.with_name(f"{mp4.stem}.segments.json"),
        mp4.with_name(f"{mp4.stem}.JSON"),
        mp4.with_name(f"{mp4.stem}.json"),
    ]
    for path in preferred:
        if path.is_file():
            return path
    return None


def list_transfer_files(card_root: Path) -> list[dict]:
    """Return files to copy from every ``DCIM/<3-digit>GOPRO`` folder.

    Primary layout: raw ``*.MP4`` plus ``*.JSON`` / ``*.segments.json`` sidecars
    directly under each GoPro folder. Legacy task folders are still included.
    """
    gopro_dirs = find_gopro_dirs(card_root)
    if not gopro_dirs:
        return []

    files: list[dict] = []
    seen_rel: set[str] = set()

    for gopro in gopro_dirs:
        try:
            entries = list(gopro.iterdir())
        except OSError:
            continue
        task_dirs = [p for p in entries if p.is_dir()]
        root_files = [p for p in entries if p.is_file() and not p.name.startswith("._")]

        # 1) Raw videos + sidecars at the GOPRO root → batch folder.
        for item in sorted(root_files, key=lambda p: p.name.lower()):
            is_mp4 = item.suffix.upper() == ".MP4"
            is_sidecar = is_json_sidecar(item.name)
            if not is_mp4 and not is_sidecar:
                continue
            try:
                size = item.stat().st_size
            except OSError:
                continue
            # Keep flat batch names; disambiguate if the same file appears in
            # more than one ###GOPRO folder on the same card.
            rel = item.name
            if rel in seen_rel:
                rel = f"{Path(item.name).stem}__{gopro.name}{Path(item.name).suffix}"
            if rel in seen_rel:
                continue
            seen_rel.add(rel)
            row = {
                "rel": rel,
                "source": str(item.resolve()),
                "size": size,
                "task": "",
            }
            if is_mp4:
                sidecar = _embed_sidecar_for(item)
                if sidecar is not None:
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
                if rel in seen_rel:
                    rel = f"{gopro.name}/{task_dir.name}/{item.name}"
                if rel in seen_rel:
                    continue
                seen_rel.add(rel)
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

"""Discover transferable MP4s + JSON sidecars under DCIM/xxxGOPRO.

Walks every nested folder (including ``100GOPRO/100GOPRO/…``). Each source
path is a unique transfer identity so two files with the same name in
different folders cannot collapse into one copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .detect import find_gopro_dirs

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


def sidecar_for_mp4(mp4: Path) -> Path | None:
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


def _embed_sidecar_for(mp4: Path) -> Path | None:
    return sidecar_for_mp4(mp4)


def _is_skipped_relative(rel: Path) -> bool:
    for part in rel.parts:
        lower = part.lower()
        if lower in SKIP_NAMES or part.startswith("."):
            return True
    return False


def _source_key(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).replace("\\", "/").lower()


def list_card_mp4_paths(card_root: Path) -> list[Path]:
    """Every ``.MP4`` under ``DCIM/<3-digit>GOPRO``, recursively."""
    found: list[Path] = []
    seen: set[str] = set()
    for gopro in find_gopro_dirs(card_root):
        try:
            for path in gopro.rglob("*"):
                if not path.is_file() or path.name.startswith("._"):
                    continue
                try:
                    rel = path.relative_to(gopro)
                except ValueError:
                    continue
                if _is_skipped_relative(rel):
                    continue
                if path.suffix.upper() != ".MP4":
                    continue
                key = _source_key(path)
                if key in seen:
                    continue
                seen.add(key)
                found.append(path)
        except OSError:
            continue
    return found


def leftover_mp4s(card_root: Path, verified_sources: list[str] | None) -> list[Path]:
    """MP4s still on the card whose source path is not in the verified set."""
    verified = {_source_key(Path(src)) for src in (verified_sources or []) if src}
    leftover: list[Path] = []
    for path in list_card_mp4_paths(card_root):
        if _source_key(path) not in verified:
            leftover.append(path)
    return leftover


def _unique_rel(gopro: Path, path: Path, seen_rel: set[str]) -> str:
    rel = path.relative_to(gopro).as_posix()
    if rel in seen_rel:
        rel = f"{gopro.name}/{rel}"
    n = 2
    original = rel
    while rel in seen_rel:
        rel = f"{Path(original).stem}__{gopro.name}_{n}{path.suffix}"
        n += 1
    return rel


def list_transfer_files(card_root: Path) -> list[dict]:
    """Return every MP4 + matching JSON under ``DCIM/<3-digit>GOPRO`` (recursive)."""
    gopro_dirs = find_gopro_dirs(card_root)
    if not gopro_dirs:
        return []

    files: list[dict] = []
    seen_rel: set[str] = set()
    seen_source: set[str] = set()

    for gopro in gopro_dirs:
        try:
            mp4s = [
                path
                for path in gopro.rglob("*")
                if path.is_file()
                and not path.name.startswith("._")
                and path.suffix.upper() == ".MP4"
                and not _is_skipped_relative(path.relative_to(gopro))
            ]
        except OSError:
            continue
        mp4s.sort(key=lambda p: p.as_posix().lower())

        for mp4 in mp4s:
            src_key = _source_key(mp4)
            if src_key in seen_source:
                continue
            seen_source.add(src_key)
            try:
                size = mp4.stat().st_size
            except OSError:
                continue
            rel = _unique_rel(gopro, mp4, seen_rel)
            seen_rel.add(rel)
            sidecar = _embed_sidecar_for(mp4)
            nested = mp4.parent != gopro
            task = mp4.parent.name if nested else ""
            try:
                source = str(mp4.resolve())
            except OSError:
                source = str(mp4)
            row = {
                "rel": rel,
                "source": source,
                "size": size,
                "task": task,
                "kind": "mp4",
            }
            if sidecar is not None:
                try:
                    row["embed_json"] = str(sidecar.resolve())
                except OSError:
                    row["embed_json"] = str(sidecar)
            files.append(row)

            if sidecar is None:
                continue
            side_key = _source_key(sidecar)
            if side_key in seen_source:
                continue
            seen_source.add(side_key)
            srel = _unique_rel(gopro, sidecar, seen_rel)
            seen_rel.add(srel)
            try:
                ssize = sidecar.stat().st_size
            except OSError:
                ssize = 0
            try:
                ssource = str(sidecar.resolve())
            except OSError:
                ssource = str(sidecar)
            files.append(
                {
                    "rel": srel,
                    "source": ssource,
                    "size": ssize,
                    "task": task,
                    "kind": "json",
                }
            )

    return files


def total_bytes(files: list[dict]) -> int:
    return sum(int(f.get("size") or 0) for f in files)


def _item_is_mp4(item: dict) -> bool:
    if str(item.get("kind") or "") == "json":
        return False
    if str(item.get("kind") or "") == "mp4":
        return True
    rel = str(item.get("rel") or "").replace("\\", "/")
    return Path(rel).suffix.upper() == ".MP4" or rel.upper().endswith(".MP4")


def unpaired_mp4s(files: list[dict]) -> list[str]:
    """MP4s that have no JSON / segments.json sidecar on the card."""
    missing: list[str] = []
    for item in files:
        if not _item_is_mp4(item):
            continue
        if item.get("embed_json"):
            continue
        missing.append(str(item.get("rel") or ""))
    return missing

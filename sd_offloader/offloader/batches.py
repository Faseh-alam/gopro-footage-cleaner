"""Discover existing batch folders already on the removable SSDs."""

from __future__ import annotations

from pathlib import Path

from .config import BATCHES_SUBDIR
from .detect import volume_free_bytes


def list_batches(ssd1: str = "", ssd2: str = "") -> list[dict]:
    """Return batches found under ``Batches/`` on either SSD.

    Flat layout: ``Batches/<batch>/*.MP4`` + ``*.segments.json``.
    Counts files in the batch folder only (no recursive size scan).
    """
    found: dict[str, dict] = {}

    for ssd in (ssd1, ssd2):
        if not ssd:
            continue
        root = Path(ssd).expanduser().resolve()
        batches_dir = root / BATCHES_SUBDIR
        if not batches_dir.is_dir():
            continue
        try:
            entries = sorted(batches_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            name = entry.name
            mp4s = 0
            jsons = 0
            try:
                for child in entry.iterdir():
                    if not child.is_file() or child.name.startswith("."):
                        continue
                    if child.suffix.upper() == ".MP4":
                        mp4s += 1
                    elif child.name.lower().endswith(".segments.json"):
                        jsons += 1
            except OSError:
                continue

            if name not in found:
                found[name] = {
                    "name": name,
                    "card_ids": [],
                    "cards": 0,
                    "mp4s": 0,
                    "jsons": 0,
                    "bytes": 0,
                    "paths": [],
                }
            row = found[name]
            row["paths"].append(str(entry))
            row["mp4s"] = int(row.get("mp4s") or 0) + mp4s
            row["jsons"] = int(row.get("jsons") or 0) + jsons
            row["cards"] = int(row["mp4s"])

    rows = sorted(found.values(), key=lambda r: r["name"].lower())
    return rows


def describe_ssd(ssd: str) -> dict | None:
    if not ssd:
        return None
    root = Path(ssd).expanduser().resolve()
    if not root.exists():
        return None
    try:
        free = volume_free_bytes(root)
    except OSError:
        free = 0
    return {"path": str(root), "free_bytes": free}

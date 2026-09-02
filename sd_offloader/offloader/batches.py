"""Discover existing batch folders already on the removable SSDs."""

from __future__ import annotations

import re
from pathlib import Path

from .config import BATCHES_SUBDIR
from .detect import volume_free_bytes


def list_batches(ssd1: str = "", ssd2: str = "") -> list[dict]:
    """Return batches found under ``Batches/`` on either SSD.

    Fast path: only lists card folders (no full-tree size scan — that blocked
    the UI for minutes on multi‑TB drives).
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
            card_ids: list[str] = []
            mp4_count = 0
            try:
                for child in entry.iterdir():
                    # Flat layout: count MP4s at the batch root.
                    if child.is_file() and child.suffix.upper() == ".MP4":
                        mp4_count += 1
                        # Collision-renamed copies end with __C1234
                        stem = child.stem.upper()
                        if "__C" in stem:
                            suffix = stem.rsplit("__", 1)[-1]
                            if suffix.startswith("C") and suffix[1:].isdigit():
                                card_ids.append(suffix)
                        continue
                    if not child.is_dir() or child.name.startswith("."):
                        continue
                    # Legacy card folders look like C1234
                    child_name = child.name.upper()
                    if len(child_name) >= 5 and child_name[0] == "C" and child_name[1:5].isdigit():
                        card_ids.append(child_name)
                    elif child_name.startswith("C") and child_name[1:].isdigit():
                        card_ids.append(child_name)
            except OSError:
                continue

            if name not in found:
                found[name] = {
                    "name": name,
                    "card_ids": [],
                    "cards": 0,
                    "files": 0,
                    "bytes": 0,
                    "paths": [],
                }
            row = found[name]
            row["paths"].append(str(entry))
            merged = sorted(set(row["card_ids"]) | set(card_ids))
            row["card_ids"] = merged
            row["cards"] = len(merged)
            row["files"] = int(row.get("files") or 0) + mp4_count

    rows = sorted(found.values(), key=lambda r: r["name"].lower())
    return rows


_BATCH_NUM_RE = re.compile(r"(\d+)\s*$")


def batch_number(name: str) -> int | None:
    text = (name or "").strip()
    match = _BATCH_NUM_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def used_batch_names(ssd1: str = "", ssd2: str = "") -> set[str]:
    return {str(row.get("name") or "") for row in list_batches(ssd1, ssd2) if row.get("name")}


def next_batch_name(ssd1: str, ssd2: str, *, seed: str = "", extra: set[str] | None = None) -> str:
    """Next unique name like ``batch 28`` that is not on either SSD or in extra."""
    used = used_batch_names(ssd1, ssd2)
    if extra:
        used |= {str(x) for x in extra if x}
    seed = (seed or "").strip()
    if seed and seed not in used:
        return seed
    numbers = [n for n in (batch_number(name) for name in used) if n is not None]
    nxt = max(numbers + [0]) + 1
    prefix = "batch "
    if seed:
        stripped = _BATCH_NUM_RE.sub("", seed).rstrip()
        prefix = f"{stripped} " if stripped else "batch "
    return f"{prefix}{nxt}".replace("  ", " ").strip()


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

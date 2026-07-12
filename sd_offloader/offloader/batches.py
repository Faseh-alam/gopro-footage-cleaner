"""Discover existing batch folders already on the removable SSDs."""

from __future__ import annotations

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
            try:
                for child in entry.iterdir():
                    if not child.is_dir() or child.name.startswith("."):
                        continue
                    # Card folders look like C1234
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
                    "bytes": 0,
                    "paths": [],
                }
            row = found[name]
            row["paths"].append(str(entry))
            merged = sorted(set(row["card_ids"]) | set(card_ids))
            row["card_ids"] = merged
            row["cards"] = len(merged)

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

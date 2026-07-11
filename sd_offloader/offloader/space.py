"""Choose which removable SSD receives the next card."""

from __future__ import annotations

from pathlib import Path

from .config import BATCHES_SUBDIR
from .detect import volume_free_bytes


def batch_root(ssd_path: str | Path, batch_name: str) -> Path:
    return Path(ssd_path).expanduser().resolve() / BATCHES_SUBDIR / batch_name.strip()


def card_dest(ssd_path: str | Path, batch_name: str, card_id: str) -> Path:
    return batch_root(ssd_path, batch_name) / card_id.upper()


def pick_ssd_for_bytes(
    *,
    ssd1: str,
    ssd2: str,
    needed_bytes: int,
    prefer: str = "ssd1",
    reserve_bytes: int = 5 * 1024**3,
) -> tuple[str, Path]:
    """Return (ssd_path_key label path, dest batch parent). Prefer ssd1 unless insufficient."""
    candidates: list[tuple[str, str]] = []
    if prefer == "ssd2":
        order = [("ssd2", ssd2), ("ssd1", ssd1)]
    else:
        order = [("ssd1", ssd1), ("ssd2", ssd2)]

    for key, path in order:
        if not path:
            continue
        root = Path(path)
        if not root.exists():
            continue
        try:
            free = volume_free_bytes(root)
        except OSError:
            continue
        if free >= needed_bytes + reserve_bytes:
            return path, root
        candidates.append((path, free))

    # Fallback: pick the one with most free space even if tight
    best_path = None
    best_free = -1
    for path, free in [(ssd1, 0), (ssd2, 0)]:
        if not path:
            continue
        try:
            free = volume_free_bytes(path)
        except OSError:
            continue
        if free > best_free:
            best_free = free
            best_path = path
    if best_path is None:
        raise RuntimeError("No SSD available — pick SSD 1 / SSD 2 in the UI")
    if best_free < needed_bytes:
        raise RuntimeError(
            f"Not enough free space on either SSD "
            f"(need {needed_bytes / (1024**3):.1f} GB, most free {best_free / (1024**3):.1f} GB)"
        )
    return best_path, Path(best_path)

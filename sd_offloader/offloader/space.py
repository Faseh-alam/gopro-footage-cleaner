"""Choose which removable SSD receives the next card.

A card is never split across SSDs. SSD1 is preferred when the card's actual
copy size fits while leaving RESERVE_BYTES free; otherwise SSD2 is tried.
"""

from __future__ import annotations

from pathlib import Path

from .config import BATCHES_SUBDIR
from .detect import volume_free_bytes

# Never consume this last slice of an SSD.
RESERVE_BYTES = 10 * 1024**3


def batch_root(ssd_path: str | Path, batch_name: str) -> Path:
    return Path(ssd_path).expanduser().resolve() / BATCHES_SUBDIR / batch_name.strip()


def path_key(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve()).lower()


def _effective_free(root: Path, reserved_bytes: dict[str, int]) -> int:
    free = volume_free_bytes(root)
    committed = int(reserved_bytes.get(path_key(root), 0) or 0)
    return int(free) - committed


def fits_with_reserve(free_bytes: int, needed_bytes: int, reserve_bytes: int = RESERVE_BYTES) -> bool:
    """True when the whole card fits and at least ``reserve_bytes`` remain."""
    if needed_bytes < 0:
        return False
    return int(free_bytes) - int(needed_bytes) >= int(reserve_bytes)


def pick_ssd_for_bytes(
    *,
    ssd1: str,
    ssd2: str,
    needed_bytes: int,
    prefer: str = "ssd1",
    reserve_bytes: int = RESERVE_BYTES,
    reserved_bytes: dict[str, int] | None = None,
) -> tuple[str, Path]:
    """Return (ssd_path, ssd_root) for one whole card.

    ``reserved_bytes`` is remaining copy size already assigned to each SSD
    (parallel / queued cards), keyed by ``path_key``.
    """
    reserved_bytes = reserved_bytes or {}
    if prefer == "ssd2":
        order = [("ssd2", ssd2), ("ssd1", ssd1)]
    else:
        order = [("ssd1", ssd1), ("ssd2", ssd2)]

    last_error = "No SSD available — pick SSD 1 / SSD 2 in the UI"
    for _key, path in order:
        if not path:
            continue
        root = Path(path)
        if not root.exists():
            continue
        try:
            effective = _effective_free(root, reserved_bytes)
        except OSError as exc:
            last_error = str(exc)
            continue
        if fits_with_reserve(effective, needed_bytes, reserve_bytes):
            return str(root.resolve()), root.resolve()
        last_error = (
            f"Not enough free space on {root} for this card "
            f"(need {needed_bytes / (1024**3):.1f} GB + "
            f"{reserve_bytes / (1024**3):.0f} GB reserve, "
            f"usable {max(0, effective) / (1024**3):.1f} GB)"
        )

    raise RuntimeError(last_error)

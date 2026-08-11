"""Derive the canonical SD card id ``C####`` from GoPro camera serials."""

from __future__ import annotations

import re
from pathlib import Path

CARD_ID_RE = re.compile(r"^C\d{4}$", re.IGNORECASE)


def card_id_from_serial(serial: str | None, fallback: str | None = None) -> str:
    """``C`` + last 4 digits of the camera serial (e.g. …0712 → C0712)."""
    digits = re.sub(r"\D", "", str(serial or "").strip())
    if len(digits) >= 4:
        return f"C{digits[-4:]}"
    badge = str(fallback or "").strip().upper()
    if CARD_ID_RE.fullmatch(badge):
        return badge
    return ""


def _iter_gopro_mp4s(card_root: Path, limit: int = 8) -> list[Path]:
    """Find a few MP4s under ``DCIM/###GOPRO`` for serial probing."""
    from .volumes import _find_gopro_root

    root = Path(card_root).expanduser()
    try:
        root = root.resolve()
    except OSError:
        return []
    gopro = _find_gopro_root(root)
    if gopro is None:
        return []
    found: list[Path] = []
    try:
        for entry in sorted(gopro.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_file() and entry.suffix.upper() == ".MP4":
                found.append(entry)
                if len(found) >= limit:
                    return found
            elif entry.is_dir():
                try:
                    for child in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                        if child.is_file() and child.suffix.upper() == ".MP4":
                            found.append(child)
                            if len(found) >= limit:
                                return found
                except OSError:
                    continue
    except OSError:
        return []
    return found


def resolve_card_identity(card_root: str | Path, fallback_name: str | None = None) -> dict:
    """Probe videos on the card for camera serial → canonical ``C####`` id."""
    root = Path(card_root)
    serial = ""
    for mp4 in _iter_gopro_mp4s(root):
        try:
            from .gopro_meta import get_media_meta

            meta = get_media_meta(mp4) or {}
            serial = str(meta.get("camera_serial") or "").strip()
            if serial:
                break
        except Exception:  # noqa: BLE001
            continue
    card_id = card_id_from_serial(serial, fallback_name)
    return {
        "card_id": card_id or str(fallback_name or "").strip().upper(),
        "camera_serial": serial or None,
        "from_serial": bool(serial and card_id_from_serial(serial)),
    }

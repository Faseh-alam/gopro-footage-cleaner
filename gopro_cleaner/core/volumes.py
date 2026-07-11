"""Cross-platform removable / local volume listing and GoPro SD card detection."""

from __future__ import annotations

import re
import string
import sys
from pathlib import Path

CARD_LABEL_RE = re.compile(r"^C\d{4}$", re.IGNORECASE)
GOPRO_DIR_RE = re.compile(r"^\d{3}GOPRO$", re.IGNORECASE)
SKIP_VOLUME_NAMES = {
    "Macintosh HD",
    "Macintosh HD - Data",
    "System",
    "Recovery",
    "EFI",
    "Home",
}


def list_volume_roots() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    home = str(Path.home())
    items.append({"name": "Home", "path": home})

    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:/")
            if root.exists():
                items.append({"name": f"{letter}:", "path": str(root)})
        return items

    volumes = Path("/Volumes")
    if volumes.exists():
        for entry in sorted(volumes.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                items.append({"name": entry.name, "path": str(entry)})
    return items


def normalize_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _windows_volume_label(letter: str) -> str:
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    result = ctypes.windll.kernel32.GetVolumeInformationW(
        f"{letter}:\\",
        buf,
        ctypes.sizeof(buf),
        None,
        None,
        None,
        None,
        0,
    )
    return buf.value.strip() if result else ""


def _find_gopro_root(root: Path) -> Path | None:
    dcim = root / "DCIM"
    if not dcim.is_dir():
        return None
    candidates: list[Path] = []
    try:
        for child in dcim.iterdir():
            if child.is_dir() and GOPRO_DIR_RE.match(child.name):
                candidates.append(child)
    except OSError:
        return None
    if not candidates:
        return None
    preferred = [p for p in candidates if p.name.upper() == "100GOPRO"]
    return preferred[0] if preferred else sorted(candidates, key=lambda p: p.name)[0]


def _card_id_for(root: Path, label: str) -> str | None:
    if CARD_LABEL_RE.match(label.strip()):
        return label.strip().upper()
    try:
        for child in root.iterdir():
            if child.is_dir() and CARD_LABEL_RE.match(child.name):
                return child.name.upper()
    except OSError:
        pass
    return None


def _scan_root_for(volume: Path, label: str, card_id: str | None) -> Path:
    """Prefer DCIM/xxxGOPRO; else C#### folder on the drive; else volume root."""
    gopro = _find_gopro_root(volume)
    if gopro is not None:
        return gopro
    if card_id and CARD_LABEL_RE.match(card_id):
        nested = volume / card_id
        if nested.is_dir():
            nested_gopro = _find_gopro_root(nested)
            if nested_gopro is not None:
                return nested_gopro
            return nested
        # Volume itself is the card but DCIM missing — still use volume
        if CARD_LABEL_RE.match(label.strip()):
            return volume
    return volume


def _iter_candidate_volumes() -> list[tuple[Path, str]]:
    """Yield (path, label) for mounted drives that might be SD cards."""
    found: list[tuple[Path, str]] = []

    if sys.platform == "win32":
        import ctypes

        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if not bitmask & 1:
                bitmask >>= 1
                continue
            bitmask >>= 1
            root = Path(f"{letter}:/")
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(root))
            # 2 = removable, 3 = fixed (some USB readers report fixed)
            if drive_type not in {2, 3}:
                continue
            if not root.exists():
                continue
            label = _windows_volume_label(letter) or letter
            found.append((root, label))
        return found

    volumes = Path("/Volumes")
    if volumes.exists():
        for entry in sorted(volumes.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            if entry.name in SKIP_VOLUME_NAMES:
                continue
            found.append((entry, entry.name))
    return found


def list_sd_cards() -> list[dict[str, str | None]]:
    """Detect GoPro SD cards named C#### (volume label or root folder).

    Returns cards with:
      - id / label: C1234
      - path: volume mount
      - scan_path: DCIM/xxxGOPRO when present (what Scan should use)
    """
    cards: list[dict[str, str | None]] = []
    seen: set[str] = set()

    for root, label in _iter_candidate_volumes():
        try:
            resolved = root.resolve()
        except OSError:
            continue
        card_id = _card_id_for(resolved, label)
        if not card_id:
            continue
        # Prefer volumes that look like GoPro media, but still list C#### labels
        gopro = _find_gopro_root(resolved)
        nested = resolved / card_id
        if gopro is None and not (nested.is_dir() or CARD_LABEL_RE.match(label.strip())):
            continue

        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)

        scan_root = _scan_root_for(resolved, label, card_id)
        cards.append(
            {
                "id": card_id,
                "label": card_id,
                "path": str(resolved),
                "scan_path": str(scan_root.resolve()),
                "gopro_root": str(gopro.resolve()) if gopro else None,
            }
        )

    cards.sort(key=lambda c: (c.get("id") or "").lower())
    return cards

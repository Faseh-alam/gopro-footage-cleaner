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


def list_storage_targets() -> list[dict]:
    """Richer drive list for the metadata inspector (system + SSD + SD cards).

    Each entry:
      name, path, kind (home|system|fixed|removable|sd_card),
      label, free_bytes?, total_bytes?, is_sd_card
    """
    import shutil

    targets: list[dict] = []
    seen: set[str] = set()

    def _add(
        *,
        name: str,
        path: Path,
        kind: str,
        label: str = "",
        is_sd_card: bool = False,
    ) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        if not resolved.exists():
            return
        key = str(resolved).lower()
        if key in seen:
            return
        seen.add(key)
        free = total = None
        try:
            usage = shutil.disk_usage(resolved)
            free, total = usage.free, usage.total
        except OSError:
            pass
        targets.append(
            {
                "name": name,
                "path": str(resolved),
                "kind": kind,
                "label": label or name,
                "is_sd_card": is_sd_card,
                "free_bytes": free,
                "total_bytes": total,
            }
        )

    home = Path.home()
    _add(name="This PC · Home", path=home, kind="home", label="Home")

    # Volume roots already claimed by an SD-card entry (avoid "SD · C2460" +
    # "Removable · E: (C2460)" for the same stick).
    sd_mounts: set[str] = set()

    def _mount_key(p: Path) -> str:
        try:
            return str(p.expanduser().resolve()).lower().rstrip("\\/")
        except OSError:
            return str(p).lower().rstrip("\\/")

    # Detected GoPro SD cards first (any volume name with DCIM/###GOPRO + MP4s)
    try:
        for card in list_sd_cards():
            mount = Path(str(card.get("path") or ""))
            scan = Path(str(card.get("scan_path") or card.get("path") or ""))
            badge = str(card.get("id") or card.get("label") or mount.name)
            # Prefer the volume root in the picker so browsing starts at the card;
            # scan_path (DCIM/###GOPRO) is still available via card detection elsewhere.
            target = mount if mount.is_dir() else scan
            _add(
                name=f"SD · {badge}",
                path=target,
                kind="sd_card",
                label=str(card.get("volume_label") or badge),
                is_sd_card=True,
            )
            if mount.is_dir():
                sd_mounts.add(_mount_key(mount))
                seen.add(_mount_key(mount))
    except Exception:  # noqa: BLE001
        pass

    if sys.platform == "win32":
        import ctypes

        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for letter in string.ascii_uppercase:
            if not bitmask & 1:
                bitmask >>= 1
                continue
            bitmask >>= 1
            root = Path(f"{letter}:/")
            if not root.exists():
                continue
            if _mount_key(root) in sd_mounts:
                continue  # already listed as SD · …
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(root))
            vol_label = _windows_volume_label(letter)
            if drive_type == 2:
                kind = "removable"
                name = f"Removable · {letter}:" + (f" ({vol_label})" if vol_label else "")
            elif drive_type == 3:
                kind = "fixed"
                name = f"Drive · {letter}:" + (f" ({vol_label})" if vol_label else "")
            else:
                kind = "system"
                name = f"{letter}:" + (f" ({vol_label})" if vol_label else "")
            _add(name=name, path=root, kind=kind, label=vol_label or letter)
    else:
        volumes = Path("/Volumes")
        if volumes.exists():
            for entry in sorted(volumes.iterdir(), key=lambda p: p.name.lower()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if _mount_key(entry) in sd_mounts:
                    continue
                if entry.name in SKIP_VOLUME_NAMES:
                    kind = "system"
                    name = f"System · {entry.name}"
                else:
                    kind = "removable"
                    name = f"Volume · {entry.name}"
                _add(name=name, path=entry, kind=kind, label=entry.name)

    # Stable order: SD cards, removable, fixed, home, system
    order = {"sd_card": 0, "removable": 1, "fixed": 2, "home": 3, "system": 4}
    targets.sort(key=lambda t: (order.get(t["kind"], 9), t["name"].lower()))
    return targets


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
    """Footage lives only in DCIM/<3-digit>GOPRO folders.

    One folder → scan it directly. Several (100GOPRO, 101GOPRO, …) → scan DCIM
    so every GOPRO folder is covered by the recursive scan.
    """
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
    if len(candidates) == 1:
        return candidates[0]
    return dcim


def _card_id_for(root: Path, label: str) -> str | None:
    """Prefer classic C#### labels; otherwise None (caller invents a display id)."""
    if CARD_LABEL_RE.match(label.strip()):
        return label.strip().upper()
    try:
        for child in root.iterdir():
            if child.is_dir() and CARD_LABEL_RE.match(child.name):
                return child.name.upper()
    except OSError:
        pass
    return None


def _display_card_id(root: Path, label: str) -> str:
    """Stable UI / registry id when the volume is not named C####.

    New cards often ship with empty / random labels — we still need something
    human-readable. Prefer C#### when present, else the volume label, else the
    drive letter / mount name.
    """
    classic = _card_id_for(root, label)
    if classic:
        return classic
    cleaned = re.sub(r"\s+", " ", (label or "").strip())
    if cleaned and cleaned.upper() not in {"", "NO NAME", "UNTITLED", "REMOVABLE DISK"}:
        # Keep readable; strip path-hostile characters.
        safe = re.sub(r'[<>:"/\\|?*]', "_", cleaned).strip(" ._")
        if safe:
            return safe[:48]
    # Windows drive letter fallback (E:/ → E)
    drive = root.drive.rstrip(":\\/") if root.drive else ""
    if drive:
        return f"CARD-{drive.upper()}"
    name = root.name.strip() or "CARD"
    return f"CARD-{name[:32]}"


def _gopro_has_mp4(gopro: Path) -> bool:
    """True if DCIM/###GOPRO (or DCIM covering several) contains any .MP4."""
    try:
        # When _find_gopro_root returns DCIM (multiple ###GOPRO folders), walk them.
        if gopro.name.upper() == "DCIM":
            folders = [
                p for p in gopro.iterdir()
                if p.is_dir() and GOPRO_DIR_RE.match(p.name)
            ]
        else:
            folders = [gopro]
        for folder in folders:
            for entry in folder.iterdir():
                if entry.is_file() and entry.suffix.upper() == ".MP4" and not entry.name.startswith("._"):
                    return True
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                # Legacy task folders under ###GOPRO
                for item in entry.iterdir():
                    if item.is_file() and item.suffix.upper() == ".MP4":
                        return True
    except OSError:
        return False
    return False


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

    return found


def list_sd_cards() -> list[dict[str, str | None]]:
    """Detect GoPro SD cards by content — not by volume name.

    Any mounted volume that has ``DCIM/<3-digit>GOPRO`` with at least one
    ``.MP4`` is a card. Volume labels may be blank / random on new cards; we
    still list them. Classic ``C####`` labels (or a ``C####`` root folder) are
    preferred as the display id when present.

    Returns cards with:
      - id / label: C1234 when available, else volume label / CARD-E
      - path: volume mount
      - scan_path: DCIM/xxxGOPRO (or DCIM if several) — what Scan should use
    """
    cards: list[dict[str, str | None]] = []
    seen: set[str] = set()

    for root, label in _iter_candidate_volumes():
        try:
            resolved = root.resolve()
        except OSError:
            continue

        gopro = _find_gopro_root(resolved)
        classic_id = _card_id_for(resolved, label)

        # Primary rule: DCIM/###GOPRO with MP4s (any volume name).
        if gopro is not None and _gopro_has_mp4(gopro):
            card_id = _display_card_id(resolved, label)
        elif classic_id:
            # Keep listing empty C####-labeled cards so operators can still pick them.
            nested = resolved / classic_id
            if gopro is None and not nested.is_dir() and not CARD_LABEL_RE.match(label.strip()):
                continue
            card_id = classic_id
        else:
            continue

        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)

        scan_root = _scan_root_for(resolved, label, classic_id)
        cards.append(
            {
                "id": card_id,
                "label": card_id,
                "volume_label": label,
                "path": str(resolved),
                "scan_path": str(scan_root.resolve()),
                "gopro_root": str(gopro.resolve()) if gopro else None,
            }
        )

    cards.sort(key=lambda c: (c.get("id") or "").lower())
    return cards

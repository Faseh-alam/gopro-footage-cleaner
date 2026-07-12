"""Volume detection for removable SSDs and GoPro SD cards."""

from __future__ import annotations

import platform
import re
import string
import subprocess
from pathlib import Path

CARD_LABEL_RE = re.compile(r"^C\d{4}$", re.IGNORECASE)
GOPRO_DIR_RE = re.compile(r"^\d{3}GOPRO$", re.IGNORECASE)
SKIP_VOLUME_NAMES = {
    "Macintosh HD",
    "Macintosh HD - Data",
    "System",
    "Recovery",
    "EFI",
}


def _windows_drives() -> list[dict]:
    import ctypes
    import shutil
    import threading

    volumes: list[dict] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for letter in string.ascii_uppercase:
        if not bitmask & 1:
            bitmask >>= 1
            continue
        bitmask >>= 1
        root = Path(f"{letter}:/")
        try:
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(root))
        except Exception:  # noqa: BLE001
            continue
        # 2 = removable, 3 = fixed (USB SSDs sometimes report fixed)
        if drive_type not in {2, 3}:
            continue

        # disk_usage can hang for minutes on empty card readers / flaky USB —
        # never block the web UI on that.
        usage_box: dict = {}

        def _usage(path: Path = root, box: dict = usage_box) -> None:
            try:
                box["usage"] = shutil.disk_usage(path)
            except OSError as exc:
                box["error"] = exc

        thread = threading.Thread(target=_usage, daemon=True)
        thread.start()
        thread.join(2.0)
        if thread.is_alive() or "usage" not in usage_box:
            continue
        usage = usage_box["usage"]
        try:
            label = _windows_volume_label(letter) or letter
        except Exception:  # noqa: BLE001
            label = letter
        try:
            gopro = _find_gopro_root(root)
        except Exception:  # noqa: BLE001
            gopro = None
        try:
            is_card = _looks_like_sd_card(root, label) if gopro else False
        except Exception:  # noqa: BLE001
            is_card = False
        try:
            card_id = _card_id_for(root, label)
        except Exception:  # noqa: BLE001
            card_id = None
        volumes.append(
            {
                "path": str(root.resolve()) if root.exists() else f"{letter}:\\",
                "label": label,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "drive_type": "removable" if drive_type == 2 else "fixed",
                "is_card_candidate": is_card,
                "card_id": card_id,
                "gopro_root": str(gopro) if gopro else None,
            }
        )
    return volumes


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


def _mac_volumes() -> list[dict]:
    import shutil

    volumes_root = Path("/Volumes")
    if not volumes_root.exists():
        return []
    volumes: list[dict] = []
    for entry in volumes_root.iterdir():
        if not entry.is_dir() or entry.name in SKIP_VOLUME_NAMES:
            continue
        if entry.name.startswith("."):
            continue
        try:
            usage = shutil.disk_usage(entry)
            resolved = entry.resolve()
        except OSError:
            continue
        label = entry.name
        volumes.append(
            {
                "path": str(resolved),
                "label": label,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "drive_type": "removable",
                "is_card_candidate": _looks_like_sd_card(resolved, label),
                "card_id": _card_id_for(resolved, label),
                "gopro_root": str(_find_gopro_root(resolved)) if _find_gopro_root(resolved) else None,
            }
        )
    return volumes


def list_volumes() -> list[dict]:
    system = platform.system()
    if system == "Windows":
        return _windows_drives()
    if system == "Darwin":
        return _mac_volumes()
    # Linux fallback
    import shutil

    volumes: list[dict] = []
    media = Path("/media")
    if media.exists():
        for user_dir in media.iterdir():
            if not user_dir.is_dir():
                continue
            for entry in user_dir.iterdir():
                if not entry.is_dir():
                    continue
                try:
                    usage = shutil.disk_usage(entry)
                except OSError:
                    continue
                label = entry.name
                volumes.append(
                    {
                        "path": str(entry.resolve()),
                        "label": label,
                        "free_bytes": usage.free,
                        "total_bytes": usage.total,
                        "drive_type": "removable",
                        "is_card_candidate": _looks_like_sd_card(entry, label),
                        "card_id": _card_id_for(entry, label),
                        "gopro_root": str(_find_gopro_root(entry)) if _find_gopro_root(entry) else None,
                    }
                )
    return volumes


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
    # Prefer 100GOPRO, else first sorted
    preferred = [p for p in candidates if p.name.upper() == "100GOPRO"]
    return preferred[0] if preferred else sorted(candidates, key=lambda p: p.name)[0]


def _card_id_for(root: Path, label: str) -> str | None:
    if CARD_LABEL_RE.match(label.strip()):
        return label.strip().upper()
    # Sometimes label is different but a C#### folder exists at root
    try:
        for child in root.iterdir():
            if child.is_dir() and CARD_LABEL_RE.match(child.name):
                return child.name.upper()
    except OSError:
        pass
    return None


def _looks_like_sd_card(root: Path, label: str) -> bool:
    if not _find_gopro_root(root):
        return False
    # Strong signal: C#### label
    if CARD_LABEL_RE.match(label.strip()):
        return True
    # Or has task-like folders under GOPRO with MP4s
    gopro = _find_gopro_root(root)
    if gopro is None:
        return False
    try:
        for folder in gopro.iterdir():
            if not folder.is_dir():
                continue
            for item in folder.iterdir():
                if item.is_file() and item.suffix.upper() == ".MP4":
                    return True
    except OSError:
        return False
    return False


def find_card_volumes(*, exclude_paths: set[str] | None = None) -> list[dict]:
    exclude = {str(Path(p).resolve()) for p in (exclude_paths or set()) if p}
    cards = []
    for vol in list_volumes():
        path = str(Path(vol["path"]).resolve())
        if path in exclude:
            continue
        if vol.get("is_card_candidate"):
            cards.append(vol)
    return cards


def volume_free_bytes(path: str | Path) -> int:
    import shutil

    return shutil.disk_usage(Path(path)).free

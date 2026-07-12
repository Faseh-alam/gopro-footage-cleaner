"""Volume detection for removable SSDs and GoPro SD cards."""

from __future__ import annotations

import platform
import re
import string
import threading
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

# Empty card readers / flaky USB can block forever on Win32 APIs.
DRIVE_PROBE_TIMEOUT_SEC = 1.5


def _run_with_timeout(fn, timeout: float = DRIVE_PROBE_TIMEOUT_SEC):
    """Run fn() in a daemon thread; return result or None on timeout/error."""
    box: dict = {}

    def worker() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive() or "error" in box or "value" not in box:
        return None
    return box["value"]


def _windows_drives() -> list[dict]:
    import ctypes

    volumes: list[dict] = []
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:  # noqa: BLE001
        return []

    for letter in string.ascii_uppercase:
        if not bitmask & 1:
            bitmask >>= 1
            continue
        bitmask >>= 1

        def probe(letter: str = letter) -> dict | None:
            import shutil

            root = Path(f"{letter}:/")
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\")
            # 2 = removable, 3 = fixed (USB SSDs sometimes report fixed)
            if drive_type not in {2, 3}:
                return None
            usage = shutil.disk_usage(f"{letter}:\\")
            label = _windows_volume_label(letter) or letter
            gopro = _find_gopro_root(root)
            is_card = _looks_like_sd_card(root, label) if gopro else False
            card_id = _card_id_for(root, label)
            path = f"{letter}:\\"
            try:
                path = str(root.resolve())
            except OSError:
                pass
            return {
                "path": path,
                "label": label,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "drive_type": "removable" if drive_type == 2 else "fixed",
                "is_card_candidate": bool(is_card),
                "card_id": card_id,
                "gopro_root": str(gopro) if gopro else None,
            }

        row = _run_with_timeout(probe, DRIVE_PROBE_TIMEOUT_SEC)
        if row:
            volumes.append(row)
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

        def probe(path: Path = entry) -> dict | None:
            usage = shutil.disk_usage(path)
            resolved = path.resolve()
            label = path.name
            gopro = _find_gopro_root(resolved)
            return {
                "path": str(resolved),
                "label": label,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "drive_type": "removable",
                "is_card_candidate": _looks_like_sd_card(resolved, label) if gopro else False,
                "card_id": _card_id_for(resolved, label),
                "gopro_root": str(gopro) if gopro else None,
            }

        row = _run_with_timeout(probe, DRIVE_PROBE_TIMEOUT_SEC)
        if row:
            volumes.append(row)
    return volumes


def list_volumes() -> list[dict]:
    system = platform.system()
    if system == "Windows":
        return _windows_drives()
    if system == "Darwin":
        return _mac_volumes()
    # Linux fallback — keep lightweight
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

                def probe(path: Path = entry) -> dict | None:
                    usage = shutil.disk_usage(path)
                    label = path.name
                    gopro = _find_gopro_root(path)
                    return {
                        "path": str(path),
                        "label": label,
                        "free_bytes": usage.free,
                        "total_bytes": usage.total,
                        "drive_type": "removable",
                        "is_card_candidate": _looks_like_sd_card(path, label) if gopro else False,
                        "card_id": _card_id_for(path, label),
                        "gopro_root": str(gopro) if gopro else None,
                    }

                row = _run_with_timeout(probe, DRIVE_PROBE_TIMEOUT_SEC)
                if row:
                    volumes.append(row)
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


def _looks_like_sd_card(root: Path, label: str) -> bool:
    if not _find_gopro_root(root):
        return False
    if CARD_LABEL_RE.match(label.strip()):
        return True
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
        try:
            path = str(Path(vol["path"]).resolve())
        except OSError:
            path = str(vol.get("path") or "")
        if path in exclude:
            continue
        if vol.get("is_card_candidate"):
            cards.append(vol)
    return cards


def volume_free_bytes(path: str | Path) -> int:
    import shutil

    result = _run_with_timeout(lambda: shutil.disk_usage(Path(path)).free, 2.0)
    if result is None:
        raise OSError(f"Timed out reading free space for {path}")
    return int(result)

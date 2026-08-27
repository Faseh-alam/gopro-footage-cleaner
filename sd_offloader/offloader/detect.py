"""Volume detection for removable SSDs and GoPro SD cards.

SD cards are identified by file structure only:

    DCIM / <3-digit>GOPRO / *.MP4 + matching *.JSON (or *.segments.json)

Volume label / card name (C001, NO NAME, UNTITLED, …) is never required.
"""

from __future__ import annotations

import platform
import re
import string
import threading
from pathlib import Path

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
            serial = _windows_volume_serial(letter)
            gopro_dirs = find_gopro_dirs(root)
            is_card = looks_like_gopro_sd(root)
            card_id = card_tracking_id(root, serial=serial, label=label)
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
                "gopro_root": str(gopro_dirs[0]) if gopro_dirs else None,
                "gopro_dirs": [str(p) for p in gopro_dirs],
                "volume_serial": serial,
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


def _windows_volume_serial(letter: str) -> str:
    """Stable media fingerprint so a new card on the same letter is detected."""
    import ctypes

    serial = ctypes.c_uint32(0)
    result = ctypes.windll.kernel32.GetVolumeInformationW(
        f"{letter}:\\",
        None,
        0,
        ctypes.byref(serial),
        None,
        None,
        None,
        0,
    )
    if not result:
        return ""
    return f"{int(serial.value):08X}"


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
            serial = str(resolved)
            gopro_dirs = find_gopro_dirs(resolved)
            return {
                "path": str(resolved),
                "label": label,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "drive_type": "removable",
                "is_card_candidate": looks_like_gopro_sd(resolved),
                "card_id": card_tracking_id(resolved, serial=serial, label=label),
                "gopro_root": str(gopro_dirs[0]) if gopro_dirs else None,
                "gopro_dirs": [str(p) for p in gopro_dirs],
                "volume_serial": serial,
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
                    serial = str(path)
                    gopro_dirs = find_gopro_dirs(path)
                    return {
                        "path": str(path),
                        "label": label,
                        "free_bytes": usage.free,
                        "total_bytes": usage.total,
                        "drive_type": "removable",
                        "is_card_candidate": looks_like_gopro_sd(path),
                        "card_id": card_tracking_id(path, serial=serial, label=label),
                        "gopro_root": str(gopro_dirs[0]) if gopro_dirs else None,
                        "gopro_dirs": [str(p) for p in gopro_dirs],
                        "volume_serial": serial,
                    }

                row = _run_with_timeout(probe, DRIVE_PROBE_TIMEOUT_SEC)
                if row:
                    volumes.append(row)
    return volumes


def find_gopro_dirs(root: Path) -> list[Path]:
    """Return every ``DCIM/<3-digit>GOPRO`` folder (e.g. 100GOPRO, 999GOPRO)."""
    dcim = root / "DCIM"
    if not dcim.is_dir():
        return []
    candidates: list[Path] = []
    try:
        for child in dcim.iterdir():
            if child.is_dir() and GOPRO_DIR_RE.match(child.name):
                candidates.append(child)
    except OSError:
        return []
    return sorted(candidates, key=lambda p: p.name.upper())


def _find_gopro_root(root: Path) -> Path | None:
    """First GoPro folder with media, else first ###GOPRO dir (compat helper)."""
    dirs = find_gopro_dirs(root)
    if not dirs:
        return None
    for folder in dirs:
        if _gopro_dir_has_media(folder):
            return folder
    return dirs[0]


def is_json_sidecar(name: str) -> bool:
    """True for ``*.segments.json`` or plain ``*.json`` (e.g. GH010001.JSON)."""
    lower = name.lower()
    return lower.endswith(".segments.json") or lower.endswith(".json")


def _sidecar_paths_for_mp4(mp4: Path) -> list[Path]:
    """Possible sidecar paths for a video (Cleaner + plain .JSON)."""
    return [
        mp4.with_name(f"{mp4.stem}.segments.json"),
        mp4.with_name(f"{mp4.stem}.scaleai.json"),
        mp4.with_name(f"{mp4.stem}.JSON"),
        mp4.with_name(f"{mp4.stem}.json"),
    ]


def _gopro_dir_has_media(gopro: Path) -> bool:
    """True when folder has ≥1 MP4 and a relevant JSON sidecar (or legacy task MP4s)."""
    try:
        entries = list(gopro.iterdir())
    except OSError:
        return False

    mp4s = [
        e
        for e in entries
        if e.is_file() and e.suffix.upper() == ".MP4" and not e.name.startswith("._")
    ]
    jsons = [e for e in entries if e.is_file() and is_json_sidecar(e.name)]

    # Primary layout: MP4 + matching .JSON / .segments.json
    for mp4 in mp4s:
        for side in _sidecar_paths_for_mp4(mp4):
            if side.is_file():
                return True
    if mp4s and jsons:
        return True

    # Legacy pre-trimmed task folders under ###GOPRO
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            children = list(entry.iterdir())
        except OSError:
            continue
        has_mp4 = any(
            c.is_file() and c.suffix.upper() == ".MP4" and not c.name.startswith("._")
            for c in children
        )
        if has_mp4:
            return True
    return False


def looks_like_gopro_sd(root: Path) -> bool:
    """Valid GoPro SD: DCIM → xxxGOPRO → MP4 + JSON. Name/label ignored."""
    for folder in find_gopro_dirs(root):
        if _gopro_dir_has_media(folder):
            return True
    return False


def card_tracking_id(root: Path, *, serial: str = "", label: str = "") -> str:
    """Stable UI/job id — never requires C####. Prefer volume serial."""
    serial = (serial or "").strip().upper()
    if serial:
        # Short, readable, unique enough across readers.
        return f"SD-{serial[-8:]}" if len(serial) >= 4 else f"SD-{serial}"

    drive = root.drive.rstrip(":\\/") if root.drive else ""
    if drive:
        return f"SD-{drive.upper()}"

    name = root.name.strip()
    if name:
        safe = re.sub(r'[<>:"/\\|?*\s]+', "-", name).strip("-_")
        if safe:
            return f"SD-{safe[:24].upper()}"
    return "SD-CARD"


# Back-compat aliases used by inventory / eject / older callers
def _looks_like_sd_card(root: Path, label: str = "") -> bool:  # noqa: ARG001
    return looks_like_gopro_sd(root)


def _card_id_for(root: Path, label: str, serial: str = "") -> str | None:
    return card_tracking_id(root, serial=serial, label=label)


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

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
            display = f"{letter}: {label}" if label.upper() != letter.upper() else f"{letter}:"
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
                "label": display,
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


def _destination_row(path: Path, *, label: str, drive_type: str) -> dict | None:
    import shutil

    try:
        resolved = path.expanduser().resolve()
        if not resolved.exists():
            return None
        usage = shutil.disk_usage(resolved)
    except OSError:
        return None
    gopro = _find_gopro_root(resolved)
    return {
        "path": str(resolved),
        "label": label,
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "drive_type": drive_type,
        "is_card_candidate": _looks_like_sd_card(resolved, label) if gopro else False,
        "card_id": _card_id_for(resolved, label) if gopro else None,
        "gopro_root": str(gopro) if gopro else None,
    }


def _local_destinations() -> list[dict]:
    """Always offer this computer as a dump target (home folder)."""
    home = Path.home()
    row = _run_with_timeout(
        lambda: _destination_row(home, label=f"This Mac — {home.name}" if platform.system() == "Darwin" else f"This PC — {home.name}", drive_type="local"),
        2.0,
    )
    return [row] if row else []


def _dedupe_volumes(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        key = str(row.get("path") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _mac_volumes() -> list[dict]:
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
            return _destination_row(path, label=path.name, drive_type="removable")

        row = _run_with_timeout(probe, DRIVE_PROBE_TIMEOUT_SEC)
        if row:
            volumes.append(row)
    return volumes


def list_volumes() -> list[dict]:
    system = platform.system()
    if system == "Windows":
        found = _windows_drives()
    elif system == "Darwin":
        found = _mac_volumes()
    else:
        found = _linux_volumes()
    return _dedupe_volumes(_local_destinations() + found)


def resolve_destination(path: str) -> dict:
    """Validate a typed folder path so it can be used as SSD 1 / SSD 2."""
    text = str(path or "").strip().strip('"')
    if not text:
        raise ValueError("Folder path is empty")
    if platform.system() == "Windows" and re.fullmatch(r"[A-Za-z]:", text):
        text = text + "\\"
    raw = Path(text).expanduser()
    if not raw.exists():
        raise ValueError(f"Folder not found: {raw}")
    if not raw.is_dir():
        raise ValueError(f"Not a folder: {raw}")
    row = _destination_row(raw, label=raw.name or str(raw), drive_type="custom")
    if not row:
        raise ValueError(f"Could not read folder: {raw}")
    return row


def browse_folder() -> dict:
    """Open a native folder picker on this PC (Windows / Mac) and return a destination."""
    import subprocess

    system = platform.system()
    if system == "Windows":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description = 'Select SSD or destination folder'; "
            "$d.ShowNewFolderButton = $true; "
            "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"
        )
        result = subprocess.run(
            ["powershell", "-STA", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
        picked = (result.stdout or "").strip()
        if not picked:
            raise ValueError("No folder selected")
        return resolve_destination(picked)
    if system == "Darwin":
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "Select SSD or destination folder")',
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise ValueError("No folder selected")
        picked = (result.stdout or "").strip()
        if not picked:
            raise ValueError("No folder selected")
        return resolve_destination(picked)
    raise ValueError("Folder picker is only available on Windows and macOS")


def _linux_volumes() -> list[dict]:
    volumes: list[dict] = []
    media = Path("/media")
    if not media.exists():
        return volumes
    for user_dir in media.iterdir():
        if not user_dir.is_dir():
            continue
        for entry in user_dir.iterdir():
            if not entry.is_dir():
                continue

            def probe(path: Path = entry) -> dict | None:
                return _destination_row(path, label=path.name, drive_type="removable")

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
    """Prefer C####; else a stable display id from volume label / drive letter.

    New cards often have blank or random names — detection is by
    ``DCIM/###GOPRO`` + MP4 content, not by the volume label.
    """
    if CARD_LABEL_RE.match(label.strip()):
        return label.strip().upper()
    try:
        for child in root.iterdir():
            if child.is_dir() and CARD_LABEL_RE.match(child.name):
                return child.name.upper()
    except OSError:
        pass
    cleaned = re.sub(r"\s+", " ", (label or "").strip())
    if cleaned and cleaned.upper() not in {"", "NO NAME", "UNTITLED", "REMOVABLE DISK"}:
        safe = re.sub(r'[<>:"/\\|?*]', "_", cleaned).strip(" ._")
        if safe:
            return safe[:48]
    drive = root.drive.rstrip(":\\/") if root.drive else ""
    if drive:
        return f"CARD-{drive.upper()}"
    name = root.name.strip()
    return f"CARD-{name[:32]}" if name else None


def _looks_like_sd_card(root: Path, label: str) -> bool:
    """Any volume with DCIM/###GOPRO containing MP4s — name does not matter."""
    del label
    gopro = _find_gopro_root(root)
    if gopro is None:
        return False
    try:
        for entry in gopro.iterdir():
            if entry.is_file() and entry.suffix.upper() == ".MP4":
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

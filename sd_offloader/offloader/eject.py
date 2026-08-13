"""Delete transferred files on the card and eject the volume."""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from .detect import _find_gopro_root
from .progress import clear_progress


def wipe_transferred_files(card_root: Path, root_files: list[str] | None = None) -> None:
    """Delete verified root MP4s + .segments.json sidecars from the GOPRO folder."""
    gopro = _find_gopro_root(card_root)
    if gopro is None:
        return
    for rel in root_files or []:
        target = gopro / rel
        if target.is_file():
            try:
                target.unlink()
            except OSError:
                pass
    clear_progress(card_root)


def wipe_transferred_tasks(
    card_root: Path, task_names: list[str], root_files: list[str] | None = None
) -> None:
    """Back-compat wrapper — task folders are no longer transferred."""
    del task_names
    wipe_transferred_files(card_root, root_files)


def eject_volume(path: str | Path) -> None:
    root = Path(path).resolve()
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["diskutil", "eject", str(root)], capture_output=True, text=True)
        return
    if system == "Windows":
        letter = root.drive.rstrip(":") or str(root)[:1]
        script = (
            f"$vol = (New-Object -ComObject Shell.Application).NameSpace(17).ParseName('{letter}:');"
            f"if ($vol) {{ $vol.InvokeVerb('Eject') }}"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
        )
        return
    subprocess.run(["umount", str(root)], capture_output=True, text=True)

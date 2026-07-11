"""Delete transferred folders on the card and eject the volume."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

from .detect import _find_gopro_root
from .progress import clear_progress


def wipe_transferred_tasks(card_root: Path, task_names: list[str]) -> None:
    gopro = _find_gopro_root(card_root)
    if gopro is None:
        return
    for name in task_names:
        folder = gopro / name
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
    clear_progress(card_root)


def eject_volume(path: str | Path) -> None:
    root = Path(path).resolve()
    system = platform.system()
    if system == "Darwin":
        # /Volumes/Name
        subprocess.run(["diskutil", "eject", str(root)], capture_output=True, text=True)
        return
    if system == "Windows":
        letter = root.drive.rstrip(":") or str(root)[:1]
        # PowerShell eject via Shell.Application
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
    # Linux best-effort
    subprocess.run(["umount", str(root)], capture_output=True, text=True)

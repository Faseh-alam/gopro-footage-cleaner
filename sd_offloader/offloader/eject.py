"""Delete transferred folders on the card and eject the volume."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

from .detect import find_gopro_dirs
from .progress import clear_progress


def wipe_transferred_tasks(
    card_root: Path, task_names: list[str], root_files: list[str] | None = None
) -> None:
    """Delete transferred legacy task folders and/or verified root files."""
    gopro_dirs = find_gopro_dirs(card_root)
    if not gopro_dirs:
        return

    for gopro in gopro_dirs:
        for name in task_names:
            folder = gopro / name
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)

    for rel in root_files or []:
        rel_path = Path(rel)
        candidates = [
            card_root / "DCIM" / rel_path,
            *[g / rel_path for g in gopro_dirs],
            *[g / rel_path.name for g in gopro_dirs],
        ]
        for target in candidates:
            if target.is_file():
                try:
                    target.unlink()
                except OSError:
                    pass
                break
    clear_progress(card_root)


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

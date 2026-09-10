"""Delete transferred folders on the card and eject the volume."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

from .detect import find_gopro_dirs
from .progress import clear_progress

# Same mapping as gopro_cleaner.core.fast_proxy: GX/GH/GP/GS video → GL proxy.
_VIDEO_PREFIXES = ("GX", "GH", "GP", "GS")
_PROXY_PREFIX = "GL"
_SIDECAR_SUFFIXES = (".segments.json", ".segments.txt")


def _clip_stem(filename: str) -> str:
    """Stem of a transferred file, treating ``.segments.json`` as one suffix."""
    name = Path(filename).name
    lower = name.lower()
    for suffix in _SIDECAR_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def _related_stems(stem: str) -> set[str]:
    stems = {stem}
    if len(stem) >= 2 and stem[:2].upper() in _VIDEO_PREFIXES:
        stems.add(_PROXY_PREFIX + stem[2:])
    return stems


def _is_companion(filename: str, stems: set[str]) -> bool:
    check = filename[2:] if filename.startswith("._") else filename
    lower = check.lower()
    return any(lower == stem.lower() or lower.startswith(stem.lower() + ".") for stem in stems)


def _wipe_companions_in_dir(folder: Path, stems: set[str]) -> None:
    if not stems:
        return
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for item in entries:
        if not item.is_file() or not _is_companion(item.name, stems):
            continue
        try:
            item.unlink()
        except OSError:
            pass


def wipe_transferred_tasks(
    card_root: Path,
    task_names: list[str],
    root_files: list[str] | None = None,
    *,
    source_paths: list[str] | None = None,
) -> None:
    """Delete transferred clips and every same-stem companion on the card.

    SSD still only receives MP4 + JSON. After verify, the SD wipe also removes
    that clip's ``.LRV`` / ``.THM`` / ``.txt`` / extra JSON and GoPro ``GL*.LRV``
    proxies. Unlabeled clips (not in the transfer list) are left alone.
    """
    gopro_dirs = find_gopro_dirs(card_root)
    if not gopro_dirs:
        return

    for gopro in gopro_dirs:
        for name in task_names:
            folder = gopro / name
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)

    by_folder: dict[Path, set[str]] = {}
    if source_paths:
        for raw in source_paths:
            src = Path(raw)
            by_folder.setdefault(src.parent, set()).update(_related_stems(_clip_stem(src.name)))
    else:
        stems: set[str] = set()
        for rel in root_files or []:
            stems.update(_related_stems(_clip_stem(Path(rel).name)))
        for gopro in gopro_dirs:
            by_folder.setdefault(gopro, set()).update(stems)

    for folder, stems in by_folder.items():
        _wipe_companions_in_dir(folder, stems)

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

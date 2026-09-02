"""Delete only verified source files on the card, then eject the volume.

Wipe never uses shutil.rmtree. If any MP4 on the card is missing from the
verified manifest, wipe is blocked.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from . import inventory
from .detect import find_gopro_dirs
from .progress import clear_progress


class WipeBlocked(RuntimeError):
    """Raised when wiping the card would risk deleting uncopied footage."""


def _under_card(path: Path, card_root: Path) -> bool:
    try:
        path.resolve().relative_to(card_root.resolve())
        return True
    except (OSError, ValueError):
        return False


def assert_wipe_allowed(card_root: Path, manifest: list[dict] | None) -> None:
    """Refuse wipe unless every manifest row is verified and no extra MP4s remain."""
    rows = list(manifest or [])
    if not rows:
        raise WipeBlocked("Wipe blocked: empty transfer manifest — SD card was not wiped")
    to_wipe = [
        r for r in rows if (bool(r.get("wipe")) if "wipe" in r else True)
    ]
    unverified = [r for r in to_wipe if not r.get("verified")]
    if unverified:
        raise WipeBlocked(
            f"Wipe blocked: {len(unverified)} file(s) not verified on SSD — "
            "SD card was not wiped"
        )
    accounted = [str(r.get("source") or "") for r in rows if r.get("source")]
    leftover = inventory.leftover_mp4s(card_root, accounted)
    if leftover:
        preview = ", ".join(p.name for p in leftover[:6])
        extra = "…" if len(leftover) > 6 else ""
        raise WipeBlocked(
            f"Wipe blocked: {len(leftover)} MP4(s) on the card were not copied/"
            f"verified ({preview}{extra}) — SD card was not wiped"
        )


def wipe_verified_sources(card_root: Path, sources: list[str] | None) -> int:
    """Unlink only the given source files. Never rmtree folders."""
    root = Path(card_root)
    deleted = 0
    for raw in sources or []:
        path = Path(raw)
        if not _under_card(path, root):
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        try:
            resolved.unlink()
            deleted += 1
        except OSError:
            pass
    clear_progress(root)
    return deleted


def wipe_transferred_tasks(
    card_root: Path, task_names: list[str], root_files: list[str] | None = None
) -> None:
    """Back-compat: delete listed root files only — never rmtree task folders."""
    del task_names  # folders are never wiped
    sources: list[str] = []
    gopro_dirs = find_gopro_dirs(card_root)
    for rel in root_files or []:
        rel_path = Path(str(rel).replace("\\", "/"))
        candidates = [
            Path(card_root) / "DCIM" / rel_path,
            *[g / rel_path for g in gopro_dirs],
            *[g / rel_path.name for g in gopro_dirs],
        ]
        for target in candidates:
            if target.is_file() and _under_card(target, Path(card_root)):
                sources.append(str(target))
                break
    wipe_verified_sources(card_root, sources)


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

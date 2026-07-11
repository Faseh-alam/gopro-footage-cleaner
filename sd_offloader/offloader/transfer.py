"""Fast file copy helpers with large buffers."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

BUFFER_SIZE = 16 * 1024 * 1024  # 16 MB


def copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    if tmp.exists():
        tmp.unlink()

    system = platform.system()
    if system == "Windows":
        # robocopy is directory-oriented; for single files use buffered copy
        _buffered_copy(src, tmp)
    elif system == "Darwin" and shutil.which("rsync"):
        # rsync single file with partial
        result = subprocess.run(
            ["rsync", "-a", "--partial", str(src), str(tmp)],
            capture_output=True,
            text=True,
        )
        if result.returncode not in {0, 23, 24}:
            # fallback
            _buffered_copy(src, tmp)
        elif not tmp.exists():
            _buffered_copy(src, tmp)
    else:
        _buffered_copy(src, tmp)

    if not tmp.exists() or tmp.stat().st_size != src.stat().st_size:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Copy size mismatch for {src.name}")
    tmp.replace(dest)


def _buffered_copy(src: Path, dest: Path) -> None:
    with src.open("rb") as reader, dest.open("wb") as writer:
        while True:
            chunk = reader.read(BUFFER_SIZE)
            if not chunk:
                break
            writer.write(chunk)
        writer.flush()

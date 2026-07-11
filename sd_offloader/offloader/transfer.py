"""Fast file copy helpers with large buffers and Windows-safe finalize."""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

BUFFER_SIZE = 16 * 1024 * 1024  # 16 MB
ProgressCallback = Callable[[int], None]  # bytes written so far for this file


def copy_file(
    src: Path,
    dest: Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            time.sleep(0.5)
            tmp.unlink(missing_ok=True)

    system = platform.system()
    if system == "Windows":
        # robocopy is directory-oriented; for single files use buffered copy
        _buffered_copy(src, tmp, on_progress=on_progress)
    elif system == "Darwin" and shutil.which("rsync") and on_progress is None:
        # rsync single file with partial (no mid-file callback)
        result = subprocess.run(
            ["rsync", "-a", "--partial", str(src), str(tmp)],
            capture_output=True,
            text=True,
        )
        if result.returncode not in {0, 23, 24}:
            _buffered_copy(src, tmp, on_progress=on_progress)
        elif not tmp.exists():
            _buffered_copy(src, tmp, on_progress=on_progress)
    else:
        _buffered_copy(src, tmp, on_progress=on_progress)

    if not tmp.exists() or tmp.stat().st_size != src.stat().st_size:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Copy size mismatch for {src.name}")
    _finalize_partial(tmp, dest)


def _finalize_partial(tmp: Path, dest: Path) -> None:
    """Rename .partial → final name, retrying Windows sharing/permission locks."""
    last_error: OSError | None = None
    for attempt in range(12):
        try:
            if dest.exists():
                dest.unlink()
            tmp.replace(dest)
            return
        except OSError as exc:
            last_error = exc
            winerr = getattr(exc, "winerror", None)
            # 32 = sharing violation, 5 = access denied; also Errno 13 Permission denied
            if winerr not in {32, 5} and exc.errno not in {13, 11, 16}:
                raise
            time.sleep(min(2.0, 0.25 * (attempt + 1)))
    raise OSError(
        f"Could not finalize {dest.name} after retries "
        f"(file may be locked by antivirus/Explorer): {last_error}"
    )


def _buffered_copy(
    src: Path,
    dest: Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> None:
    written = 0
    last_report = 0.0
    with src.open("rb") as reader, dest.open("wb") as writer:
        while True:
            chunk = reader.read(BUFFER_SIZE)
            if not chunk:
                break
            writer.write(chunk)
            written += len(chunk)
            now = time.time()
            # Report every ~16MB chunk, but at least every 0.5s for UI smoothness
            if on_progress and (now - last_report >= 0.5 or len(chunk) < BUFFER_SIZE):
                on_progress(written)
                last_report = now
        writer.flush()
        try:
            writer.flush()
        except OSError:
            pass
    if on_progress:
        on_progress(written)

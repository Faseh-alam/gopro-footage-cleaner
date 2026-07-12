"""Resolve ffmpeg / ffprobe binaries with clear Windows-friendly errors.

Falls back to the ``static-ffmpeg`` pip package (downloaded on first use) so
``run.bat`` / ``run.sh`` do not require a system FFmpeg install.
"""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache
from pathlib import Path

_MISSING_HINT = (
    "FFmpeg is not available. Pull latest, run run.bat / run.sh again "
    "(installs the static-ffmpeg package and downloads binaries on first start). "
    "Or install system FFmpeg and restart. Test: ffmpeg -version"
)


class FFmpegNotFoundError(RuntimeError):
    """Raised when ffmpeg or ffprobe cannot be located."""


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("FFMPEG_PATH") or os.environ.get("GOPRO_FFMPEG_PATH")
    if env:
        dirs.append(Path(env))
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        program = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        for base in (
            Path(program) / "ffmpeg" / "bin",
            Path(program_x86) / "ffmpeg" / "bin",
            Path(local) / "Microsoft" / "WinGet" / "Links",
            Path(local) / "Programs" / "ffmpeg" / "bin",
            Path(r"C:\ffmpeg\bin"),
            Path.home() / "ffmpeg" / "bin",
            Path.home() / "scoop" / "shims",
            Path.home() / "scoop" / "apps" / "ffmpeg" / "current" / "bin",
        ):
            dirs.append(base)
    else:
        dirs.extend(
            [
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
                Path("/usr/bin"),
            ]
        )
    return dirs


@lru_cache(maxsize=1)
def _static_ffmpeg_bins() -> tuple[str, str] | None:
    """Download/use pip static-ffmpeg binaries (ffmpeg, ffprobe)."""
    try:
        import static_ffmpeg
        from static_ffmpeg import run

        static_ffmpeg.add_paths(weak=True)
        ffmpeg, ffprobe = run.get_or_fetch_platform_executables_else_raise()
        return str(ffmpeg), str(ffprobe)
    except Exception:
        return None


def ensure_ffmpeg(*, quiet: bool = False) -> dict:
    """Make sure ffmpeg/ffprobe exist (system PATH or static-ffmpeg download)."""
    status = ffmpeg_available()
    if status["ok"]:
        return status
    # Clear cache and force a static-ffmpeg fetch attempt.
    _static_ffmpeg_bins.cache_clear()
    resolve_binary.cache_clear()
    if not quiet:
        print("Downloading FFmpeg binaries (one-time, via static-ffmpeg)...")
    bins = _static_ffmpeg_bins()
    if bins:
        resolve_binary.cache_clear()
        return ffmpeg_available()
    return ffmpeg_available()


@lru_cache(maxsize=2)
def resolve_binary(name: str) -> str:
    """Return absolute path to ``ffmpeg`` or ``ffprobe``, or raise FFmpegNotFoundError."""
    exe = f"{name}.exe" if sys.platform == "win32" else name
    found = shutil.which(name) or shutil.which(exe)
    if found:
        return found
    for folder in _candidate_dirs():
        candidate = folder / exe
        if candidate.is_file():
            return str(candidate.resolve())

    bins = _static_ffmpeg_bins()
    if bins:
        ffmpeg, ffprobe = bins
        if name == "ffmpeg" and Path(ffmpeg).is_file():
            return ffmpeg
        if name == "ffprobe" and Path(ffprobe).is_file():
            return ffprobe

    raise FFmpegNotFoundError(_MISSING_HINT)


def ffmpeg_bin() -> str:
    return resolve_binary("ffmpeg")


def ffprobe_bin() -> str:
    return resolve_binary("ffprobe")


def ffmpeg_available() -> dict:
    try:
        ff = ffmpeg_bin()
        fp = ffprobe_bin()
        return {"ok": True, "ffmpeg": ff, "ffprobe": fp, "hint": ""}
    except FFmpegNotFoundError as exc:
        return {"ok": False, "ffmpeg": None, "ffprobe": None, "hint": str(exc)}

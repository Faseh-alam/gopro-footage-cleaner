"""Probe MP4 durations with ffprobe (for batch hours accounting)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_VIDEO_EXT = {".mp4", ".mov", ".m4v"}


def _ffprobe() -> str:
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from gopro_cleaner.core.ffmpeg_tools import ffprobe_bin  # type: ignore

        return ffprobe_bin()
    except Exception:  # noqa: BLE001
        return "ffprobe"


def probe_seconds(path: Path) -> float:
    """Return duration in seconds for one media file, or 0 on failure."""
    path = Path(path)
    if not path.is_file():
        return 0.0
    cmd = [
        _ffprobe(),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    if result.returncode != 0:
        return 0.0
    try:
        return max(0.0, float((result.stdout or "").strip()))
    except ValueError:
        return 0.0


def sum_video_seconds(paths: list[Path]) -> tuple[float, int, int]:
    """Sum durations. Returns (seconds, ok_count, fail_count)."""
    total = 0.0
    ok = 0
    fail = 0
    for path in paths:
        if path.suffix.lower() not in _VIDEO_EXT:
            continue
        secs = probe_seconds(path)
        if secs > 0:
            total += secs
            ok += 1
        else:
            fail += 1
    return total, ok, fail


def video_paths_under(root: Path, rel_paths: list[str] | None = None) -> list[Path]:
    if rel_paths is not None:
        out = []
        for rel in rel_paths:
            p = root / rel
            if p.suffix.lower() in _VIDEO_EXT:
                out.append(p)
        return out
    if not root.is_dir():
        return []
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in _VIDEO_EXT]

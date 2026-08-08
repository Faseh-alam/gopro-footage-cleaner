"""Encode a short review clip as a WhatsApp-friendly MP4 (H.264 + AAC).

Fast encode + bitrate caps keep files small enough to share while staying sharp
enough to show camera-angle issues. Quality presets: 720p or 1080p (default).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .ffmpeg_tools import ffmpeg_bin
from .probe import probe_media

# Practical upper bound for a "share this issue" clip.
MAX_SHARE_SECONDS = 300.0
MIN_SHARE_SECONDS = 0.25

# height, crf, maxrate, bufsize, audio bitrate
QUALITY_PRESETS: dict[str, tuple[int, int, str, str, str]] = {
    "720p": (720, 24, "2500k", "5000k", "96k"),
    "1080p": (1080, 23, "4000k", "8000k", "128k"),
}
DEFAULT_QUALITY = "1080p"


def _safe_stem(name: str) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r"[^\w.\-]+", "_", stem).strip("._")
    return cleaned or "clip"


def normalize_quality(quality: str | None) -> str:
    key = (quality or DEFAULT_QUALITY).strip().lower()
    if key in {"720", "720p", "hd"}:
        return "720p"
    if key in {"1080", "1080p", "fhd", "fullhd"}:
        return "1080p"
    raise ValueError("quality must be 720p or 1080p")


def build_share_clip(
    source: Path,
    start_seconds: float,
    end_seconds: float,
    *,
    quality: str = DEFAULT_QUALITY,
    output_path: Path | None = None,
) -> Path:
    """Trim + re-encode ``source[start:end]`` for WhatsApp download.

    Returns the path to the encoded MP4. Caller owns cleanup when using a temp
    path (the default).
    """
    quality = normalize_quality(quality)
    height, crf, maxrate, bufsize, audio_br = QUALITY_PRESETS[quality]

    source = source.expanduser().resolve(strict=True)
    if end_seconds <= start_seconds + MIN_SHARE_SECONDS:
        raise ValueError("End time must be after start (at least 0.25s)")
    duration = end_seconds - start_seconds
    if duration > MAX_SHARE_SECONDS:
        raise ValueError(
            f"Share clip is too long ({duration:.0f}s). Keep it under {int(MAX_SHARE_SECONDS)}s."
        )

    media = probe_media(source)
    if media.duration is not None and start_seconds >= media.duration:
        raise ValueError("Start time is past the end of the video")
    if media.duration is not None and end_seconds > media.duration + 0.5:
        end_seconds = float(media.duration)
        duration = end_seconds - start_seconds

    if output_path is None:
        fd, name = tempfile.mkstemp(
            suffix=".mp4",
            prefix=f"wa_{quality}_{_safe_stem(source.name)}_{int(time.time())}_",
        )
        os.close(fd)
        output_path = Path(name)
    else:
        output_path = output_path.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

    # ultrafast + bitrate caps: ~a few MB for a 12s clip, encodes in seconds.
    # -ss before -i for fast keyframe seek on large GoPro sources.
    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_seconds:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-vf",
        (
            f"scale='min({height * 16 // 9},iw)':'min({height},ih)'"
            ":force_original_aspect_ratio=decrease,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        ),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        str(crf),
        "-maxrate",
        maxrate,
        "-bufsize",
        bufsize,
        "-profile:v",
        "main",
        "-level",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        audio_br,
        "-ac",
        "2",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        "-threads",
        "0",
        str(output_path),
    ]

    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        from .ffmpeg_tools import FFmpegNotFoundError

        raise FFmpegNotFoundError(
            "FFmpeg is not installed or not on PATH. Install FFmpeg and restart."
        ) from exc

    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
        if output_path.exists():
            try:
                output_path.unlink()
            except OSError:
                pass
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(err or "ffmpeg failed while building the share clip")

    return output_path


def download_filename(
    source: Path,
    start_seconds: float,
    end_seconds: float,
    *,
    quality: str = DEFAULT_QUALITY,
) -> str:
    stem = _safe_stem(source.name)
    a = int(max(0, start_seconds))
    b = int(max(0, end_seconds))
    q = normalize_quality(quality)
    return f"{stem}_share_{q}_{a}-{b}.mp4"

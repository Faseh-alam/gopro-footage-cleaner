"""Inspect GoPro media files with ffprobe."""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".360", ".mkv"}
GOPRO_PREFIXES = ("GOPR", "GP", "GX", "GL", "GH")

# Cache ffprobe results keyed by (path, mtime, size) — probing the same file
# repeatedly is expensive on slow USB drives and happens on every status poll.
_PROBE_CACHE_MAX = 512
_probe_cache: dict[str, tuple[int, int, "MediaInfo"]] = {}
_probe_cache_lock = threading.Lock()


@dataclass(frozen=True)
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str | None
    codec_tag: str | None
    handler_name: str | None


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float | None
    size_bytes: int
    streams: list[StreamInfo]
    video_index: int | None
    audio_index: int | None
    gpmf_index: int | None
    has_gpmf: bool


def is_video_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS


def looks_like_gopro(path: Path) -> bool:
    stem = path.stem.upper()
    return any(stem.startswith(prefix) for prefix in GOPRO_PREFIXES)


def _run_ffprobe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "ffprobe failed"
        raise RuntimeError(f"Could not inspect {path.name}: {stderr}")
    return json.loads(result.stdout)


def _is_gpmf_stream(stream: dict) -> bool:
    tag = (stream.get("codec_tag_string") or "").lower()
    handler = (stream.get("tags", {}).get("handler_name") or "").strip()
    return tag == "gpmd" or "gopro met" in handler.lower()


def probe_media(path: Path) -> MediaInfo:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    stat = path.stat()
    cache_key = str(path)
    with _probe_cache_lock:
        hit = _probe_cache.get(cache_key)
        if hit and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
            return hit[2]

    payload = _run_ffprobe(path)
    streams: list[StreamInfo] = []
    video_index = None
    audio_index = None
    gpmf_index = None

    for stream in payload.get("streams", []):
        info = StreamInfo(
            index=stream["index"],
            codec_type=stream.get("codec_type", ""),
            codec_name=stream.get("codec_name"),
            codec_tag=stream.get("codec_tag_string"),
            handler_name=stream.get("tags", {}).get("handler_name"),
        )
        streams.append(info)
        if info.codec_type == "video" and video_index is None:
            video_index = info.index
        elif info.codec_type == "audio" and audio_index is None:
            audio_index = info.index
        elif _is_gpmf_stream(stream):
            gpmf_index = info.index

    duration_raw = payload.get("format", {}).get("duration")
    duration = float(duration_raw) if duration_raw is not None else None

    info = MediaInfo(
        path=path,
        duration=duration,
        size_bytes=stat.st_size,
        streams=streams,
        video_index=video_index,
        audio_index=audio_index,
        gpmf_index=gpmf_index,
        has_gpmf=gpmf_index is not None,
    )
    with _probe_cache_lock:
        if len(_probe_cache) >= _PROBE_CACHE_MAX:
            _probe_cache.pop(next(iter(_probe_cache)))
        _probe_cache[cache_key] = (stat.st_mtime_ns, stat.st_size, info)
    return info

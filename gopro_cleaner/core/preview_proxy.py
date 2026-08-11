"""Lightweight 720p preview proxies for reviewing large GoPro files.

Originals stay untouched for trim/export. Previews are encoded as HLS (1-second
segments + playlist) so the player can start smooth 8× playback within a couple
of seconds instead of waiting for the whole file to finish transcoding.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import threading
from pathlib import Path

_lock = threading.Lock()
_jobs: dict[str, dict] = {}

# Bump when encoder settings change so old caches are ignored.
_PREVIEW_VERSION = "v8-fast-720p"

_PLAYLIST_NAME = "index.m3u8"
# Playable as soon as the first segment exists (~1s of video).
_MIN_PLAYABLE_SEGMENTS = 1

# Optional override: set GOPRO_PREVIEW_DISABLED=1 to force originals only.
def _previews_disabled() -> bool:
    return os.environ.get("GOPRO_PREVIEW_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def _cache_dir() -> Path:
    path = Path.home() / ".cache" / "gopro-cleaner" / "previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def preview_cache_root() -> Path:
    """Public accessor for HTTP routes serving HLS assets."""
    return _cache_dir()


def _cache_key(source: Path) -> str:
    stat = source.stat()
    digest = hashlib.sha256(
        f"{_PREVIEW_VERSION}:{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    )
    return digest.hexdigest()[:20]


def _preview_dir(source: Path) -> Path:
    return _cache_dir() / _cache_key(source)


def _playlist_path(source: Path) -> Path:
    return _preview_dir(source) / _PLAYLIST_NAME


def _hls_url(source: Path) -> str:
    return f"/api/eager/preview/hls/{_cache_key(source)}/{_PLAYLIST_NAME}"


def _playlist_state(playlist: Path) -> tuple[int, bool]:
    """Return (segment_count, finished) for a playlist file (0, False if absent)."""
    try:
        text = playlist.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, False
    segments = sum(1 for line in text.splitlines() if line.strip().endswith(".ts"))
    return segments, "#EXT-X-ENDLIST" in text


def _probe_duration_seconds(source: Path) -> float:
    try:
        from .ffmpeg_tools import ffprobe_bin

        result = subprocess.run(
            [
                ffprobe_bin(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return max(0.0, float((result.stdout or "").strip() or 0))
    except Exception:
        return 0.0


_SOFTWARE_ENCODER_ARGS = [
    # ultrafast + higher CRF: prioritize encode speed for review proxies.
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-tune",
    "fastdecode",
    "-crf",
    "32",
    "-threads",
    "0",
]

_WINDOWS_HW_CANDIDATES: list[tuple[str, list[str]]] = [
    ("nvenc", ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-b:v", "900k", "-maxrate", "1200k", "-bufsize", "1800k"]),
    ("qsv", ["-c:v", "h264_qsv", "-preset", "veryfast", "-b:v", "900k"]),
    ("amf", ["-c:v", "h264_amf", "-quality", "speed", "-b:v", "900k"]),
]

# Probed once per process: hardware encode is 3–10× faster than x264 on big files.
_win_encoder_args: list[str] | None = None


def _encoder_works(encoder_args: list[str]) -> bool:
    """Tiny null-sink test encode — proves the encoder exists AND the GPU accepts it."""
    from .ffmpeg_tools import ffmpeg_bin

    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=black:s=256x256:d=0.2:r=12",
        *encoder_args,
        "-frames:v",
        "3",
        "-f",
        "null",
        "-",
    ]
    try:
        return subprocess.run(command, capture_output=True, timeout=20, check=False).returncode == 0
    except Exception:
        return False


def _preview_encoder_args() -> list[str]:
    """Prefer hardware encode when available; otherwise fast software x264."""
    system = platform.system()
    if system == "Darwin":
        return [
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            "900k",
            "-maxrate",
            "1200k",
            "-bufsize",
            "1800k",
        ]
    if system == "Windows":
        global _win_encoder_args
        prefer = (os.environ.get("GOPRO_PREVIEW_ENCODER") or "auto").strip().lower()
        if prefer in {"x264", "libx264", "software", "cpu"}:
            return list(_SOFTWARE_ENCODER_ARGS)
        for name, args in _WINDOWS_HW_CANDIDATES:
            if prefer in {name, f"h264_{name}"}:
                return list(args)
        if _win_encoder_args is None:
            _win_encoder_args = next(
                (args for _, args in _WINDOWS_HW_CANDIDATES if _encoder_works(args)),
                list(_SOFTWARE_ENCODER_ARGS),
            )
        return list(_win_encoder_args)
    return list(_SOFTWARE_ENCODER_ARGS)


def _hwaccel_input_args() -> list[str]:
    if platform.system() == "Darwin":
        return ["-hwaccel", "videotoolbox"]
    if platform.system() == "Windows":
        return ["-hwaccel", "auto"]
    return []


def _hls_output_args(dest_dir: Path) -> list[str]:
    """Write the preview as 1s HLS segments — playable almost immediately."""
    return [
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "0",
        "-hls_playlist_type",
        "event",
        # temp_file: segments appear atomically, never half-written to the player.
        "-hls_flags",
        "temp_file+independent_segments",
        "-hls_segment_filename",
        str(dest_dir / "seg%05d.ts"),
        str(dest_dir / _PLAYLIST_NAME),
    ]


def _build_preview(source: Path, dest_dir: Path, job_key: str, process_holder: list) -> None:
    from .ffmpeg_tools import ffmpeg_bin

    duration = _probe_duration_seconds(source)
    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *_hwaccel_input_args(),
        "-i",
        str(source),
        "-an",
        "-vf",
        # 720p @ 10fps — ultrafast CRF; playable after first 1s HLS segment.
        "scale='min(720,iw)':-2:flags=fast_bilinear,fps=10",
        *_preview_encoder_args(),
        "-pix_fmt",
        "yuv420p",
        "-g",
        "10",
        "-keyint_min",
        "10",
        "-sc_threshold",
        "0",
        "-progress",
        "pipe:1",
        "-nostats",
        *_hls_output_args(dest_dir),
    ]
    # Normal priority — preview encode should finish as fast as the machine allows.
    popen_kwargs: dict = {}
    stderr_log = dest_dir / "stderr.log"
    stderr_handle = open(stderr_log, "w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=stderr_handle,
        text=True,
        bufsize=1,
        **popen_kwargs,
    )
    process_holder.append(process)
    with _lock:
        if job_key in _jobs:
            _jobs[job_key]["process"] = process

    assert process.stdout is not None
    for line in process.stdout:
        with _lock:
            job = _jobs.get(job_key)
            if not job or job.get("status") != "running":
                process.terminate()
                break
        # NOTE: ffmpeg's out_time_ms is misnamed — the value is MICROseconds
        # (same as out_time_us). Treating it as ms made progress hit the 99%
        # cap ~1000× early, so the UI sat on "Finalizing" for the whole build.
        if line.startswith(("out_time_us=", "out_time_ms=")) and duration > 0:
            try:
                out_us = int(line.split("=", 1)[1].strip() or 0)
                pct = min(99, int((out_us / 1_000_000.0) / duration * 100))
            except ValueError:
                continue
            with _lock:
                if job_key in _jobs and _jobs[job_key].get("status") == "running":
                    _jobs[job_key]["progress"] = max(int(_jobs[job_key].get("progress") or 0), pct)

    code = process.wait()
    try:
        stderr_handle.close()
    except OSError:
        pass
    err = ""
    try:
        err = stderr_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        err = ""
    finally:
        try:
            stderr_log.unlink()
        except OSError:
            pass
    if code != 0:
        # Auto-fallback: if the HW encoder failed mid-run, retry once with libx264.
        joined = " ".join(command)
        if any(hw in joined for hw in ("nvenc", "qsv", "amf")):
            _clear_dir_contents(dest_dir)
            soft = [
                ffmpeg_bin(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-an",
                "-vf",
                "scale='min(720,iw)':-2:flags=fast_bilinear,fps=12",
                *_SOFTWARE_ENCODER_ARGS,
                "-pix_fmt",
                "yuv420p",
                "-g",
                "12",
                "-keyint_min",
                "12",
                "-sc_threshold",
                "0",
                *_hls_output_args(dest_dir),
            ]
            retry = subprocess.run(soft, capture_output=True, text=True, check=False)
            segments, finished = _playlist_state(dest_dir / _PLAYLIST_NAME)
            if retry.returncode == 0 and segments > 0 and finished:
                return
            err = (retry.stderr or err or "").strip()
        raise RuntimeError(err.strip() or "ffmpeg failed while building preview")


def _clear_dir_contents(directory: Path) -> None:
    try:
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def cancel_preview(source: Path) -> None:
    source = source.expanduser().resolve()
    key = str(source)
    with _lock:
        job = _jobs.get(key)
        if not job:
            return
        proc = job.get("process")
        job["status"] = "cancelled"
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        _jobs.pop(key, None)
    # Drop the half-built segment folder so a later start rebuilds cleanly.
    try:
        playlist = _playlist_path(source)
        if not _playlist_state(playlist)[1]:
            shutil.rmtree(playlist.parent, ignore_errors=True)
    except OSError:
        pass


def _public_job(job: dict, extra: dict | None = None) -> dict:
    """Only JSON-safe fields — the raw job dict holds Thread/Popen objects."""
    out = {
        key: job.get(key)
        for key in (
            "status",
            "progress",
            "path",
            "hls",
            "playable",
            "cached",
            "error",
            "message",
            "source_bytes",
        )
        if job.get(key) is not None
    }
    if extra:
        out.update(extra)
    return out


def preview_status(source: Path, *, start: bool = False) -> dict:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    key = str(source)
    size = source.stat().st_size
    playlist = _playlist_path(source)
    hls_url = _hls_url(source)
    segments, finished = _playlist_state(playlist)
    if finished and segments > 0:
        return {
            "status": "ready",
            "path": str(playlist),
            "hls": hls_url,
            "playable": True,
            "cached": True,
            "progress": 100,
            "source_bytes": size,
        }

    if _previews_disabled():
        return {
            "status": "skipped",
            "reason": "disabled",
            "message": "Preview proxies disabled (GOPRO_PREVIEW_DISABLED)",
            "source_bytes": size,
        }

    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") == "running":
            # Playable a few seconds into the encode — the UI attaches HLS then.
            return _public_job(
                job,
                {
                    "source_bytes": size,
                    "hls": hls_url,
                    "segments": segments,
                    "playable": segments >= _MIN_PLAYABLE_SEGMENTS,
                },
            )
        if job and job.get("status") in {"error", "cancelled"}:
            _jobs.pop(key, None)

    if not start:
        return {"status": "idle", "progress": 0, "source_bytes": size}

    with _lock:
        _jobs[key] = {
            "status": "running",
            "progress": 0,
            "process": None,
            "source_bytes": size,
            "message": "Building 720p preview for smooth review…",
        }

    def worker() -> None:
        dest_dir = playlist.parent
        process_holder: list = []
        try:
            # Interrupted/stale builds leave a playlist without ENDLIST — rebuild.
            _clear_dir_contents(dest_dir)
            _build_preview(source, dest_dir, key, process_holder)
            with _lock:
                if _jobs.get(key, {}).get("status") != "running":
                    shutil.rmtree(dest_dir, ignore_errors=True)
                    return
            seg_count, done = _playlist_state(playlist)
            if seg_count <= 0 or not done:
                raise RuntimeError("ffmpeg finished but the preview playlist is incomplete")
            with _lock:
                _jobs[key] = {
                    "status": "ready",
                    "path": str(playlist),
                    "hls": hls_url,
                    "playable": True,
                    "cached": False,
                    "progress": 100,
                    "process": None,
                    "source_bytes": size,
                    "message": "Preview ready",
                }
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(dest_dir, ignore_errors=True)
            with _lock:
                if _jobs.get(key, {}).get("status") == "running":
                    _jobs[key] = {
                        "status": "error",
                        "error": str(exc),
                        "progress": 0,
                        "process": None,
                        "source_bytes": size,
                    }

    thread = threading.Thread(target=worker, daemon=True, name=f"preview-{source.name}")
    thread.start()
    with _lock:
        if key in _jobs:
            _jobs[key]["thread"] = thread
    return {"status": "running", "progress": 0, "source_bytes": size, "message": "Building 720p preview…"}


def resolve_preview(source: Path) -> Path:
    """Path to the finished preview playlist (raises while still building)."""
    status = preview_status(source, start=False)
    if status.get("status") == "ready":
        return Path(status["path"])
    raise RuntimeError(status.get("error") or "Preview not ready")

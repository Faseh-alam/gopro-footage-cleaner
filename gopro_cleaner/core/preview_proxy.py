"""Lightweight 720p preview proxies for reviewing large GoPro files.

Originals stay untouched for trim/export. Review playback prefers the proxy once
ready, and falls back to HTTP range streaming of the original while it builds.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import threading
from pathlib import Path

_lock = threading.Lock()
_jobs: dict[str, dict] = {}

# Bump when encoder settings change so old caches are ignored.
_PREVIEW_VERSION = "v6-review-720p"

# Optional override: set GOPRO_PREVIEW_DISABLED=1 to force originals only.
def _previews_disabled() -> bool:
    return os.environ.get("GOPRO_PREVIEW_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def _cache_dir() -> Path:
    path = Path.home() / ".cache" / "gopro-cleaner" / "previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(source: Path) -> str:
    stat = source.stat()
    digest = hashlib.sha256(
        f"{_PREVIEW_VERSION}:{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    )
    return digest.hexdigest()[:20]


def _cached_preview_path(source: Path) -> Path:
    return _cache_dir() / f"{_cache_key(source)}.mp4"


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


def _preview_encoder_args() -> list[str]:
    """Prefer hardware encode when available; otherwise fast software x264."""
    system = platform.system()
    if system == "Darwin":
        return [
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            "1200k",
            "-maxrate",
            "1500k",
            "-bufsize",
            "3000k",
        ]
    if system == "Windows":
        # NVENC is much faster on big GoPro files when a GPU is present.
        # Fall back path is selected at build time if this encoder is missing.
        prefer = (os.environ.get("GOPRO_PREVIEW_ENCODER") or "auto").strip().lower()
        if prefer in {"nvenc", "h264_nvenc"}:
            return ["-c:v", "h264_nvenc", "-preset", "p1", "-b:v", "1400k", "-maxrate", "1800k", "-bufsize", "2800k"]
        if prefer in {"qsv", "h264_qsv"}:
            return ["-c:v", "h264_qsv", "-preset", "veryfast", "-b:v", "1400k"]
    # ultrafast + moderate CRF: small proxy, quick encode, still scrubbable at 8×
    return [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "fastdecode",
        "-crf",
        "28",
        "-threads",
        "0",
    ]


def _hwaccel_input_args() -> list[str]:
    if platform.system() == "Darwin":
        return ["-hwaccel", "videotoolbox"]
    if platform.system() == "Windows":
        return ["-hwaccel", "auto"]
    return []


def _build_preview(source: Path, dest: Path, job_key: str, process_holder: list) -> None:
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
        # 720p, 12fps — tiny decode cost so 5–8× review feels smooth.
        "scale='min(720,iw)':-2:flags=fast_bilinear,fps=12",
        *_preview_encoder_args(),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-g",
        "12",
        "-keyint_min",
        "12",
        "-sc_threshold",
        "0",
        "-progress",
        "pipe:1",
        "-nostats",
        str(dest),
    ]
    # Run below normal priority so the encode never starves live playback.
    popen_kwargs: dict = {}
    if platform.system() == "Windows":
        popen_kwargs["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    else:
        popen_kwargs["preexec_fn"] = lambda: os.nice(10)  # noqa: PLW1509
    # stderr must go to a file, not a PIPE — an unread PIPE fills up and
    # deadlocks ffmpeg mid-encode (build then hangs forever at N%).
    stderr_log = dest.with_suffix(".stderr.log")
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
        if line.startswith("out_time_ms=") and duration > 0:
            try:
                out_ms = int(line.split("=", 1)[1].strip() or 0)
                pct = min(99, int((out_ms / 1000.0) / duration * 100))
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
        # Auto-fallback: if NVENC/QSV unavailable, retry once with libx264.
        if "nvenc" in " ".join(command) or "qsv" in " ".join(command):
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
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "fastdecode",
                "-crf",
                "28",
                "-threads",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-g",
                "12",
                "-keyint_min",
                "12",
                "-sc_threshold",
                "0",
                str(dest),
            ]
            retry = subprocess.run(soft, capture_output=True, text=True, check=False)
            if retry.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                return
            err = (retry.stderr or err or "").strip()
        raise RuntimeError(err.strip() or "ffmpeg failed while building preview")


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


def _public_job(job: dict, extra: dict | None = None) -> dict:
    """Only JSON-safe fields — the raw job dict holds Thread/Popen objects."""
    out = {
        key: job.get(key)
        for key in ("status", "progress", "path", "cached", "error", "message", "source_bytes", "preview_bytes")
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
    cached = _cached_preview_path(source)
    if cached.exists() and cached.stat().st_size > 0:
        return {
            "status": "ready",
            "path": str(cached),
            "cached": True,
            "progress": 100,
            "source_bytes": size,
            "preview_bytes": cached.stat().st_size,
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
        if job and job.get("status") == "ready" and Path(job["path"]).exists():
            return _public_job(job)
        if job and job.get("status") == "running":
            return _public_job(job, {"source_bytes": size})
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
        temp = cached.with_suffix(".part.mp4")
        process_holder: list = []
        try:
            if temp.exists():
                temp.unlink()
            _build_preview(source, temp, key, process_holder)
            with _lock:
                if _jobs.get(key, {}).get("status") != "running":
                    if temp.exists():
                        temp.unlink()
                    return
            if cached.exists():
                cached.unlink()
            temp.replace(cached)
            with _lock:
                _jobs[key] = {
                    "status": "ready",
                    "path": str(cached),
                    "cached": False,
                    "progress": 100,
                    "process": None,
                    "source_bytes": size,
                    "preview_bytes": cached.stat().st_size,
                    "message": "Preview ready",
                }
        except Exception as exc:  # noqa: BLE001
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
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
    status = preview_status(source, start=False)
    if status.get("status") == "ready":
        return Path(status["path"])
    raise RuntimeError(status.get("error") or "Preview not ready")

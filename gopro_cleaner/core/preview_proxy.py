"""Low-resolution preview proxies for fast review scrubbing."""

from __future__ import annotations

import hashlib
import platform
import subprocess
import threading
from pathlib import Path

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _cache_dir() -> Path:
    path = Path.home() / ".cache" / "gopro-cleaner" / "previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


_PREVIEW_VERSION = "v2-1080p"


def _cache_key(source: Path) -> str:
    stat = source.stat()
    digest = hashlib.sha256(
        f"{_PREVIEW_VERSION}:{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    )
    return digest.hexdigest()[:20]


def _cached_preview_path(source: Path) -> Path:
    return _cache_dir() / f"{_cache_key(source)}.mp4"


def _preview_encoder_args() -> list[str]:
    # 1080p review proxy — readable detail without full GoPro file weight.
    if platform.system() == "Darwin":
        return ["-c:v", "h264_videotoolbox", "-b:v", "4500k", "-maxrate", "5500k", "-bufsize", "9000k"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]


def _hwaccel_input_args() -> list[str]:
    if platform.system() == "Darwin":
        return ["-hwaccel", "videotoolbox"]
    return []


def _build_preview(source: Path, dest: Path, job_key: str) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *_hwaccel_input_args(),
        "-i",
        str(source),
        "-an",
        "-vf",
        "scale='min(1920,iw)':-2,fps=24",
        *_preview_encoder_args(),
        "-movflags",
        "+faststart",
        "-g",
        "48",
        "-keyint_min",
        "48",
        "-progress",
        "pipe:1",
        "-nostats",
        str(dest),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    source_size = max(source.stat().st_size, 1)
  # Rough target: ~12% of source size for the 1080p proxy file.
    target_size = max(source_size * 0.12, 30_000_000)
    for line in process.stdout:
        if line.startswith("out_time_ms="):
            try:
                out_ms = int(line.split("=", 1)[1].strip())
            except ValueError:
                continue
            if dest.exists():
                pct = min(95, int(dest.stat().st_size / target_size * 100))
                with _lock:
                    if job_key in _jobs and _jobs[job_key].get("status") == "running":
                        _jobs[job_key]["progress"] = max(_jobs[job_key].get("progress", 0), pct)
    code = process.wait()
    if code != 0:
        raise RuntimeError("ffmpeg failed while building preview")


def preview_status(source: Path) -> dict:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    key = str(source)
    cached = _cached_preview_path(source)
    if cached.exists() and cached.stat().st_size > 0:
        return {"status": "ready", "path": str(cached), "cached": True, "progress": 100}

    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") == "ready" and Path(job["path"]).exists():
            return job
        if job and job.get("status") == "running":
            return job
        if job and job.get("status") == "error":
            return job
        _jobs[key] = {"status": "running", "progress": 0}

    def worker() -> None:
        temp = cached.with_suffix(".part.mp4")
        try:
            if temp.exists():
                temp.unlink()
            _build_preview(source, temp, key)
            if cached.exists():
                cached.unlink()
            temp.replace(cached)
            with _lock:
                _jobs[key] = {
                    "status": "ready",
                    "path": str(cached),
                    "cached": False,
                    "progress": 100,
                }
        except Exception as exc:  # noqa: BLE001
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
            with _lock:
                _jobs[key] = {"status": "error", "error": str(exc), "progress": 0}

    threading.Thread(target=worker, daemon=True, name=f"preview-{source.name}").start()
    return {"status": "running", "progress": 0}


def resolve_preview(source: Path) -> Path:
    status = preview_status(source)
    if status.get("status") == "ready":
        return Path(status["path"])
    raise RuntimeError(status.get("error") or "Preview not ready")

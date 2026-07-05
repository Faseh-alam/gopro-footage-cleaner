"""Lightweight preview proxies for review scrubbing (optional, off by default for large files)."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import threading
from pathlib import Path

_lock = threading.Lock()
_jobs: dict[str, dict] = {}

# Do not auto-build previews for huge GoPro files — scrub the original instead.
MAX_PREVIEW_BYTES = int(os.environ.get("GOPRO_PREVIEW_MAX_MB", "1500")) * 1_000_000


def _cache_dir() -> Path:
    path = Path.home() / ".cache" / "gopro-cleaner" / "previews"
    path.mkdir(parents=True, exist_ok=True)
    return path


_PREVIEW_VERSION = "v4-lite-480p"


def _cache_key(source: Path) -> str:
    stat = source.stat()
    digest = hashlib.sha256(
        f"{_PREVIEW_VERSION}:{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    )
    return digest.hexdigest()[:20]


def _cached_preview_path(source: Path) -> Path:
    return _cache_dir() / f"{_cache_key(source)}.mp4"


def _preview_encoder_args() -> list[str]:
    if platform.system() == "Darwin":
        return ["-c:v", "h264_videotoolbox", "-b:v", "900k", "-maxrate", "1100k", "-bufsize", "2200k"]
  # ultrafast + high CRF = smallest/fastest proxy on weak Windows PCs
    return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-crf", "30", "-threads", "2"]


def _hwaccel_input_args() -> list[str]:
    if platform.system() == "Darwin":
        return ["-hwaccel", "videotoolbox"]
    if platform.system() == "Windows":
        return ["-hwaccel", "auto"]
    return []


def _build_preview(source: Path, dest: Path, job_key: str, process_holder: list) -> None:
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
        "scale='min(480,iw)':-2,fps=10",
        *_preview_encoder_args(),
        "-movflags",
        "+faststart",
        "-g",
        "30",
        "-keyint_min",
        "30",
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
    process_holder.append(process)
    with _lock:
        if job_key in _jobs:
            _jobs[job_key]["process"] = process
    assert process.stdout is not None
    source_size = max(source.stat().st_size, 1)
    target_size = max(source_size * 0.03, 8_000_000)
    for line in process.stdout:
        with _lock:
            job = _jobs.get(job_key)
            if not job or job.get("status") != "running":
                process.terminate()
                break
        if line.startswith("out_time_ms="):
            if dest.exists():
                pct = min(95, int(dest.stat().st_size / target_size * 100))
                with _lock:
                    if job_key in _jobs and _jobs[job_key].get("status") == "running":
                        _jobs[job_key]["progress"] = max(_jobs[job_key].get("progress", 0), pct)
    code = process.wait()
    if code != 0:
        raise RuntimeError("ffmpeg failed while building preview")


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


def preview_status(source: Path, *, start: bool = False) -> dict:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    key = str(source)
    size = source.stat().st_size
    cached = _cached_preview_path(source)
    if cached.exists() and cached.stat().st_size > 0:
        return {"status": "ready", "path": str(cached), "cached": True, "progress": 100}

    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") == "ready" and Path(job["path"]).exists():
            return job
        if job and job.get("status") == "running":
            return job
        if job and job.get("status") in {"error", "cancelled"}:
            _jobs.pop(key, None)

    if not start:
        if size > MAX_PREVIEW_BYTES:
            return {
                "status": "skipped",
                "reason": "large_file",
                "message": "File too large for background preview — scrubbing original",
            }
        return {"status": "idle", "progress": 0}

    if size > MAX_PREVIEW_BYTES:
        return {
            "status": "skipped",
            "reason": "large_file",
            "message": "File too large for background preview — scrubbing original",
        }

    with _lock:
        _jobs[key] = {"status": "running", "progress": 0, "process": None}

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
                }
        except Exception as exc:  # noqa: BLE001
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass
            with _lock:
                if _jobs.get(key, {}).get("status") == "running":
                    _jobs[key] = {"status": "error", "error": str(exc), "progress": 0, "process": None}

    thread = threading.Thread(target=worker, daemon=True, name=f"preview-{source.name}")
    thread.start()
    with _lock:
        if key in _jobs:
            _jobs[key]["thread"] = thread
    return {"status": "running", "progress": 0}


def resolve_preview(source: Path) -> Path:
    status = preview_status(source, start=False)
    if status.get("status") == "ready":
        return Path(status["path"])
    raise RuntimeError(status.get("error") or "Preview not ready")

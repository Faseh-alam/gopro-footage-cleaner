"""Snapshot filmstrip for fast garbage spotting (adaptive interval from clip duration)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

from .probe import probe_media

_lock = threading.Lock()
_jobs: dict[str, dict] = {}

GARBAGE_RATIO = 0.10
GARBAGE_SNAPSHOT_THRESHOLD = 4
INTERVAL_MIN_SEC = 5.0
MAX_SNAPSHOTS = 120
SNAPSHOT_WIDTH = 320


def _cache_root() -> Path:
    path = Path.home() / ".cache" / "gopro-cleaner" / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(source: Path) -> str:
    stat = source.stat()
    digest = hashlib.sha256(
        f"v2-10pct:{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    )
    return digest.hexdigest()[:20]


def _snapshot_dir(source: Path) -> Path:
    return _cache_root() / _cache_key(source)


def _manifest_path(source: Path) -> Path:
    return _snapshot_dir(source) / "manifest.json"


def compute_snapshot_interval(duration_seconds: float) -> float:
    """Spacing so GARBAGE_SNAPSHOT_THRESHOLD idle frames ≈ 10% of clip length."""
    if not duration_seconds or duration_seconds <= 0:
        return INTERVAL_MIN_SEC
    max_garbage = duration_seconds * GARBAGE_RATIO
    interval = max_garbage / GARBAGE_SNAPSHOT_THRESHOLD
    interval = max(INTERVAL_MIN_SEC, interval)
    count = int(duration_seconds / interval) + 1
    if count > MAX_SNAPSHOTS:
        interval = duration_seconds / max(MAX_SNAPSHOTS - 1, 1)
        interval = max(INTERVAL_MIN_SEC, interval)
    return round(interval, 2)


def snapshot_times(duration_seconds: float, interval: float | None = None) -> list[float]:
    if not duration_seconds or duration_seconds <= 0:
        return [0.0]
    step = interval if interval is not None else compute_snapshot_interval(duration_seconds)
    times: list[float] = []
    t = 0.0
    while t < duration_seconds - 0.05:
        times.append(round(t, 3))
        t += step
    if not times or times[-1] < duration_seconds - step * 0.5:
        times.append(round(min(duration_seconds - 0.04, times[-1] + step if times else 0), 3))
    return times


def snapshot_plan(source: Path) -> dict:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    try:
        duration = probe_media(source).duration or 0.0
    except (RuntimeError, OSError):
        duration = 0.0
    interval = compute_snapshot_interval(duration)
    times = snapshot_times(duration, interval)
    max_garbage = round(duration * GARBAGE_RATIO, 1) if duration else 0
    return {
        "path": str(source),
        "duration": duration,
        "interval_seconds": interval,
        "snapshot_count": len(times),
        "garbage_threshold": GARBAGE_SNAPSHOT_THRESHOLD,
        "max_garbage_seconds": max_garbage,
        "garbage_hint": (
            f"{GARBAGE_SNAPSHOT_THRESHOLD}+ snapshots in a row without work "
            f"≈ garbage (up to ~{int(max_garbage)}s allowed in this clip)"
        ),
        "times": times,
    }


def _load_manifest(source: Path) -> dict | None:
    path = _manifest_path(source)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _build_snapshots(source: Path, job_key: str) -> None:
    source = source.expanduser().resolve()
    plan = snapshot_plan(source)
    interval = plan["interval_seconds"]
    out_dir = _snapshot_dir(source)
    out_dir.mkdir(parents=True, exist_ok=True)

    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()

    pattern = str(out_dir / "frame_%05d.jpg")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"fps=1/{interval},scale={SNAPSHOT_WIDTH}:-1",
        "-q:v",
        "7",
        "-progress",
        "pipe:1",
        "-nostats",
        pattern,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    with _lock:
        if job_key in _jobs:
            _jobs[job_key]["process"] = process

    expected = max(plan["snapshot_count"], 1)
    assert process.stdout is not None
    for line in process.stdout:
        with _lock:
            if _jobs.get(job_key, {}).get("status") != "running":
                process.terminate()
                break
        if line.startswith("frame="):
            try:
                frame_no = int(line.split("=", 1)[1].strip())
            except ValueError:
                continue
            pct = min(95, int(frame_no / expected * 100))
            with _lock:
                if job_key in _jobs and _jobs[job_key].get("status") == "running":
                    _jobs[job_key]["progress"] = max(_jobs[job_key].get("progress", 0), pct)

    code = process.wait()
    if code != 0:
        raise RuntimeError("ffmpeg failed while extracting snapshots")

    frames = sorted(out_dir.glob("frame_*.jpg"), key=lambda p: p.name)
    times = plan["times"]
    manifest_frames = []
    for index, frame_path in enumerate(frames):
        t = times[index] if index < len(times) else index * interval
        manifest_frames.append({"index": index, "t": t, "file": frame_path.name})

    manifest = {
        **plan,
        "frames": manifest_frames,
        "ready": True,
    }
    _manifest_path(source).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def snapshot_status(source: Path, *, start: bool = False) -> dict:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    key = str(source)
    manifest = _load_manifest(source)
    if manifest and manifest.get("ready") and manifest.get("frames"):
        return {
            "status": "ready",
            "progress": 100,
            "cached": True,
            "manifest": manifest,
        }

    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") == "running":
            return {k: v for k, v in job.items() if k != "process"}
        if job and job.get("status") == "error":
            return job

    plan = snapshot_plan(source)
    if not start:
        return {"status": "idle", "progress": 0, "plan": plan}

    with _lock:
        _jobs[key] = {"status": "running", "progress": 0, "plan": plan}

    def worker() -> None:
        try:
            _build_snapshots(source, key)
            manifest = _load_manifest(source)
            with _lock:
                _jobs[key] = {
                    "status": "ready",
                    "progress": 100,
                    "cached": False,
                    "manifest": manifest,
                }
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _jobs[key] = {"status": "error", "error": str(exc), "progress": 0}

    threading.Thread(target=worker, daemon=True, name=f"snapshots-{source.name}").start()
    return {"status": "running", "progress": 0, "plan": plan}


def cancel_snapshots(source: Path) -> None:
    source = source.expanduser().resolve()
    key = str(source)
    with _lock:
        job = _jobs.get(key)
        if job:
            proc = job.get("process")
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


def resolve_snapshot_frame(source: Path, index: int) -> Path:
    manifest = _load_manifest(source)
    if not manifest or not manifest.get("frames"):
        raise RuntimeError("Snapshots not ready")
    for frame in manifest["frames"]:
        if frame["index"] == index:
            path = _snapshot_dir(source) / frame["file"]
            if path.exists():
                return path
            break
    raise FileNotFoundError(f"Snapshot frame {index} not found")

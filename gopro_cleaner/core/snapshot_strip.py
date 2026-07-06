"""Snapshot filmstrip — adaptive spacing for cleaning, fixed 5s spacing for labeling."""

from __future__ import annotations

import hashlib
import json
import platform
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
LABEL_PREVIEW_COUNT = 8
LABEL_PREVIEW_INTERVAL_SEC = 5.0
LABEL_PREVIEW_SPAN_SEC = 40.0
SNAPSHOT_WIDTH = 240
MIN_FRAMES_BEFORE_UI = 3


def _normalize_purpose(purpose: str) -> str:
    value = purpose.strip().lower()
    return value if value in {"clean", "label"} else "clean"


def _job_key(source: Path, purpose: str) -> str:
    return f"{source.expanduser().resolve()}:{_normalize_purpose(purpose)}"


def _cache_root() -> Path:
    path = Path.home() / ".cache" / "gopro-cleaner" / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_key(source: Path, purpose: str) -> str:
    purpose = _normalize_purpose(purpose)
    stat = source.stat()
    version = "v5-label-preview" if purpose == "label" else "v3-fastseek"
    digest = hashlib.sha256(
        f"{version}:{purpose}:{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    )
    return digest.hexdigest()[:20]


def _snapshot_dir(source: Path, purpose: str) -> Path:
    return _cache_root() / _cache_key(source, purpose)


def _manifest_path(source: Path, purpose: str) -> Path:
    return _snapshot_dir(source, purpose) / "manifest.json"


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


def snapshot_plan(source: Path, *, purpose: str = "clean") -> dict:
    source = source.expanduser().resolve()
    purpose = _normalize_purpose(purpose)
    if not source.exists():
        raise FileNotFoundError(source)
    try:
        duration = probe_media(source).duration or 0.0
    except (RuntimeError, OSError):
        duration = 0.0

    if purpose == "label":
        interval = LABEL_PREVIEW_INTERVAL_SEC
        times: list[float] = []
        t = 0.0
        while t < min(duration, LABEL_PREVIEW_SPAN_SEC) - 0.05 and len(times) < LABEL_PREVIEW_COUNT:
            times.append(round(t, 3))
            t += interval
        if not times:
            times = [0.0]
        garbage_hint = (
            f"Opening preview ({len(times)} shots) — use , . and ±3s to scrub through the clip"
        )
        max_garbage = 0
    else:
        interval = compute_snapshot_interval(duration)
        times = snapshot_times(duration, interval)
        max_garbage = round(duration * GARBAGE_RATIO, 1) if duration else 0
        garbage_hint = (
            f"{GARBAGE_SNAPSHOT_THRESHOLD}+ snapshots in a row without work "
            f"≈ garbage (up to ~{int(max_garbage)}s allowed in this clip)"
        )

    return {
        "path": str(source),
        "purpose": purpose,
        "duration": duration,
        "interval_seconds": interval,
        "snapshot_count": len(times),
        "garbage_threshold": GARBAGE_SNAPSHOT_THRESHOLD if purpose == "clean" else 0,
        "max_garbage_seconds": max_garbage,
        "garbage_hint": garbage_hint,
        "times": times,
    }


def _load_manifest(source: Path, purpose: str) -> dict | None:
    path = _manifest_path(source, purpose)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _hwaccel_args() -> list[str]:
    if platform.system() == "Darwin":
        return ["-hwaccel", "videotoolbox"]
    if platform.system() == "Windows":
        return ["-hwaccel", "auto"]
    return []


def _extract_frame(source: Path, timestamp: float, dest: Path) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *_hwaccel_args(),
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-an",
        "-vf",
        f"scale={SNAPSHOT_WIDTH}:-1",
        "-q:v",
        "8",
        str(dest),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffmpeg frame extract failed")


def _job_cancelled(job_key: str) -> bool:
    with _lock:
        job = _jobs.get(job_key)
        return not job or job.get("status") != "running"


def _update_job_progress(job_key: str, plan: dict, frames: list[dict], pct: int) -> None:
    partial = {**plan, "frames": list(frames), "ready": False, "partial": True}
    with _lock:
        if job_key in _jobs and _jobs[job_key].get("status") == "running":
            _jobs[job_key]["progress"] = pct
            _jobs[job_key]["manifest"] = partial


def _build_snapshots(source: Path, job_key: str, purpose: str) -> None:
    source = source.expanduser().resolve()
    purpose = _normalize_purpose(purpose)
    plan = snapshot_plan(source, purpose=purpose)
    times = plan["times"]
    out_dir = _snapshot_dir(source, purpose)
    out_dir.mkdir(parents=True, exist_ok=True)

    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()
    manifest_file = _manifest_path(source, purpose)
    if manifest_file.exists():
        manifest_file.unlink()

    total = max(len(times), 1)
    frames: list[dict] = []

    for index, timestamp in enumerate(times):
        if _job_cancelled(job_key):
            return

        dest = out_dir / f"frame_{index:05d}.jpg"
        _extract_frame(source, timestamp, dest)
        frames.append({"index": index, "t": timestamp, "file": dest.name})
        pct = min(99, int((index + 1) / total * 100))
        _update_job_progress(job_key, plan, frames, pct)

    manifest = {**plan, "frames": frames, "ready": True, "partial": False}
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def snapshot_status(source: Path, *, start: bool = False, purpose: str = "clean") -> dict:
    source = source.expanduser().resolve()
    purpose = _normalize_purpose(purpose)
    if not source.exists():
        raise FileNotFoundError(source)

    key = _job_key(source, purpose)
    manifest = _load_manifest(source, purpose)
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
            result = {k: v for k, v in job.items() if k not in {"process"}}
            return result
        if job and job.get("status") == "error":
            return job

    plan = snapshot_plan(source, purpose=purpose)
    if not start:
        return {"status": "idle", "progress": 0, "plan": plan}

    with _lock:
        _jobs[key] = {"status": "running", "progress": 0, "plan": plan}

    def worker() -> None:
        try:
            _build_snapshots(source, key, purpose)
            manifest = _load_manifest(source, purpose)
            with _lock:
                if _jobs.get(key, {}).get("status") != "running":
                    return
                _jobs[key] = {
                    "status": "ready",
                    "progress": 100,
                    "cached": False,
                    "manifest": manifest,
                }
        except Exception as exc:  # noqa: BLE001
            with _lock:
                if _jobs.get(key, {}).get("status") == "running":
                    _jobs[key] = {"status": "error", "error": str(exc), "progress": 0}

    threading.Thread(target=worker, daemon=True, name=f"snapshots-{source.name}").start()
    return {"status": "running", "progress": 0, "plan": plan}


def cancel_snapshots(source: Path, *, purpose: str | None = None) -> None:
    source = source.expanduser().resolve()
    keys: list[str]
    if purpose is not None:
        keys = [_job_key(source, purpose)]
    else:
        keys = [_job_key(source, "clean"), _job_key(source, "label")]

    with _lock:
        for key in keys:
            job = _jobs.get(key)
            if not job:
                continue
            job["status"] = "cancelled"
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


def resolve_snapshot_frame(source: Path, index: int, *, purpose: str = "clean") -> Path:
    purpose = _normalize_purpose(purpose)
    manifest = _load_manifest(source, purpose)
    frames = (manifest or {}).get("frames") or []

    with _lock:
        job = _jobs.get(_job_key(source, purpose))
        if job and job.get("manifest"):
            frames = job["manifest"].get("frames") or frames

    if not frames:
        raise RuntimeError("Snapshots not ready")
    for frame in frames:
        if frame["index"] == index:
            path = _snapshot_dir(source, purpose) / frame["file"]
            if path.exists():
                return path
            break
    raise FileNotFoundError(f"Snapshot frame {index} not found")

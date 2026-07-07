"""Snapshot filmstrip — adaptive spacing for cleaning, fixed 5s spacing for labeling."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import threading
import time
from collections import deque
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
SNAPSHOT_WIDTH = 200
MIN_FRAMES_BEFORE_UI = 3
SNAPSHOT_FFMPEG_THREADS = 1
PRIORITY_FOREGROUND = 10
PRIORITY_BACKGROUND = 1

_ffmpeg_lock = threading.Lock()
_queue: deque[tuple[int, float, str, Path, str]] = deque()
_queue_keys: set[str] = set()
_queue_worker_started = False


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
    version = "v7-queue"
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


def _manifest_frames_valid(source: Path, purpose: str, manifest: dict) -> bool:
    frames = manifest.get("frames") or []
    if not frames:
        return False
    out_dir = _snapshot_dir(source, purpose)
    return all(_frame_looks_valid(out_dir / frame["file"]) for frame in frames)


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
    return []


def _snapshot_video_filter() -> str:
    return f"scale={SNAPSHOT_WIDTH}:-2:flags=fast_bilinear,format=yuvj420p"


def _frame_looks_valid(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return path.stat().st_size >= 256
    except OSError:
        return False


def _input_strategies(source: Path, timestamp: float) -> list[list[str]]:
    source_arg = str(source)
    coarse = max(0.0, timestamp - 2.0)
    fine = min(2.0, timestamp)
    return [
        ["-ss", f"{coarse:.3f}", "-i", source_arg, "-ss", f"{fine:.3f}"],
        ["-ss", f"{timestamp:.3f}", "-i", source_arg],
        ["-i", source_arg, "-ss", f"{timestamp:.3f}"],
    ]


def _extract_frame(source: Path, timestamp: float, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    errors: list[str] = []
    vf = _snapshot_video_filter()
    for hwaccel in ([True, False] if _hwaccel_args() else [False]):
        for input_args in _input_strategies(source, timestamp):
            command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-threads",
                str(SNAPSHOT_FFMPEG_THREADS),
            ]
            if hwaccel:
                command.extend(_hwaccel_args())
            command.extend(
                input_args
                + [
                    "-frames:v",
                    "1",
                    "-an",
                    "-vf",
                    vf,
                    "-pix_fmt",
                    "yuvj420p",
                    "-q:v",
                    "9",
                    str(dest),
                ]
            )
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0 and _frame_looks_valid(dest):
                return
            if dest.exists():
                dest.unlink(missing_ok=True)
            detail = result.stderr.strip() or "ffmpeg frame extract failed"
            label = "hwaccel" if hwaccel else "software"
            errors.append(f"{label}: {detail}")

    raise RuntimeError(errors[-1] if errors else "ffmpeg frame extract failed")


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
    failures = 0

    with _ffmpeg_lock:
        for index, timestamp in enumerate(times):
            if _job_cancelled(job_key):
                return

            dest = out_dir / f"frame_{index:05d}.jpg"
            try:
                _extract_frame(source, timestamp, dest)
            except RuntimeError:
                failures += 1
                continue

            frames.append({"index": index, "t": timestamp, "file": dest.name})
            pct = min(99, int((index + 1) / total * 100))
            _update_job_progress(job_key, plan, frames, pct)

    min_required = 1 if purpose == "label" else MIN_FRAMES_BEFORE_UI
    if len(frames) < min_required:
        raise RuntimeError(
            f"Only {len(frames)}/{len(times)} snapshots could be built"
            + (f" ({failures} failed)" if failures else "")
        )

    manifest = {
        **plan,
        "frames": frames,
        "ready": True,
        "partial": len(frames) < len(times),
    }
    manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _parse_priority(raw: str | int | None) -> int:
    if raw is None:
        return PRIORITY_BACKGROUND
    if isinstance(raw, int):
        return raw
    value = str(raw).strip().lower()
    if value in {"foreground", "high", "urgent"}:
        return PRIORITY_FOREGROUND
    if value.isdigit():
        return int(value)
    return PRIORITY_BACKGROUND


def _enqueue_snapshot(source: Path, purpose: str, *, priority: int) -> None:
    global _queue_worker_started
    key = _job_key(source, purpose)
    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") in {"running", "ready"}:
            if priority > job.get("priority", PRIORITY_BACKGROUND):
                job["priority"] = priority
            return

        if key in _queue_keys:
            rebuilt: deque[tuple[int, float, str, Path, str]] = deque()
            for neg_priority, queued_at, queued_key, queued_source, queued_purpose in _queue:
                if queued_key == key:
                    neg_priority = min(neg_priority, -priority)
                    queued_source = source
                    queued_purpose = purpose
                rebuilt.append((neg_priority, queued_at, queued_key, queued_source, queued_purpose))
            _queue.clear()
            _queue.extend(sorted(rebuilt, key=lambda item: (item[0], item[1])))
            if job and job.get("status") == "queued":
                job["priority"] = max(job.get("priority", PRIORITY_BACKGROUND), priority)
            return

        _queue.append((-priority, time.time(), key, source, purpose))
        _queue_keys.add(key)
        if not _queue_worker_started:
            _queue_worker_started = True
            threading.Thread(
                target=_queue_worker_loop,
                daemon=True,
                name="snapshot-queue",
            ).start()


def _queue_worker_loop() -> None:
    while True:
        with _lock:
            if not _queue:
                item = None
            else:
                _, _, key, source, purpose = _queue.popleft()
                _queue_keys.discard(key)
                item = (key, source, purpose)

        if item is None:
            time.sleep(0.25)
            continue

        key, source, purpose = item
        with _lock:
            if key in _jobs and _jobs[key].get("status") in {"running", "ready"}:
                continue
            plan = snapshot_plan(source, purpose=purpose)
            _jobs[key] = {
                "status": "running",
                "progress": 0,
                "plan": plan,
                "priority": PRIORITY_FOREGROUND,
            }

        try:
            _build_snapshots(source, key, purpose)
            manifest = _load_manifest(source, purpose)
            with _lock:
                if _jobs.get(key, {}).get("status") != "running":
                    continue
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


def snapshot_status(
    source: Path,
    *,
    start: bool = False,
    purpose: str = "clean",
    priority: str | int | None = None,
) -> dict:
    source = source.expanduser().resolve()
    purpose = _normalize_purpose(purpose)
    if not source.exists():
        raise FileNotFoundError(source)

    key = _job_key(source, purpose)
    manifest = _load_manifest(source, purpose)
    if manifest and manifest.get("ready") and manifest.get("frames"):
        if _manifest_frames_valid(source, purpose, manifest):
            return {
                "status": "ready",
                "progress": 100,
                "cached": True,
                "manifest": manifest,
            }
        manifest_file = _manifest_path(source, purpose)
        manifest_file.unlink(missing_ok=True)

    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") in {"running", "queued"}:
            result = {k: v for k, v in job.items() if k not in {"process"}}
            return result
        if job and job.get("status") == "error":
            if not start:
                return job

    plan = snapshot_plan(source, purpose=purpose)
    if not start:
        return {"status": "idle", "progress": 0, "plan": plan}

    prio = _parse_priority(priority)
    with _lock:
        _jobs[key] = {"status": "queued", "progress": 0, "plan": plan, "priority": prio}
    _enqueue_snapshot(source, purpose, priority=prio)
    return {"status": "queued", "progress": 0, "plan": plan}


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
            if _frame_looks_valid(path):
                return path
            break
    raise FileNotFoundError(f"Snapshot frame {index} not found")

"""Snapshot filmstrip — adaptive spacing for cleaning, fixed spacing for labeling."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .lite_mode import lite_mode_enabled, performance_config
from .probe import probe_media
from .snapshot_settings import load_snapshot_settings

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _settings() -> dict:
    return load_snapshot_settings()


def _snapshot_width() -> int:
    return int(_settings()["snapshot_width"])


def _jpeg_quality() -> int:
    return int(_settings()["jpeg_quality"])


def _label_preview_count() -> int:
    return int(_settings()["label_preview_count"])


def _ffmpeg_timeout() -> int:
    return 30 if lite_mode_enabled() else 45


MIN_FRAMES_BEFORE_UI = 3
SNAPSHOT_FFMPEG_THREADS = 2
PRIORITY_FOREGROUND = 10
PRIORITY_BACKGROUND = 1
BOOTSTRAP_FRAMES = 3

_ffmpeg_lock = threading.Lock()
_queue: deque[tuple[int, float, str, Path, str]] = deque()
_queue_keys: set[str] = set()
_queue_worker_started = False
_active_job_key: str | None = None
_current_ffmpeg_proc: subprocess.Popen[str] | None = None


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
    snap = _settings()
    version = (
        f"v12-{snap['garbage_percent']}-{snap['resolution_factor']}"
        f"-{snap['min_interval_sec']}-{snap['max_interval_sec']}"
        f"-{'lite' if lite_mode_enabled() else 'full'}"
    )
    digest = hashlib.sha256(
        f"{version}:{purpose}:{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    )
    return digest.hexdigest()[:20]


def _snapshot_dir(source: Path, purpose: str) -> Path:
    return _cache_root() / _cache_key(source, purpose)


def _manifest_path(source: Path, purpose: str) -> Path:
    return _snapshot_dir(source, purpose) / "manifest.json"


def _format_minutes(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    rest = int(seconds % 60)
    return f"{minutes}m {rest}s" if rest else f"{minutes} min"


def get_snapshot_timestamps(
    clip_duration_seconds: float,
    garbage_percent: float = 0.10,
    N: int = 3,
    *,
    min_interval: float | None = None,
    max_interval: float | None = None,
) -> list[float]:
    """Return filmstrip timestamps for a clip.

    1. tolerance_seconds = clip_duration_seconds * garbage_percent
    2. raw_interval = tolerance_seconds / N
    3. snapshot_interval = clamp(raw_interval, MIN_INTERVAL, MAX_INTERVAL)
    4. num_snapshots = ceil(clip_duration_seconds / snapshot_interval)
    5. timestamps = 0, interval, 2*interval, ... (num_snapshots points, clamped to duration)
    """
    snap = _settings()
    min_iv = float(snap["min_interval_sec"] if min_interval is None else min_interval)
    max_iv = float(snap["max_interval_sec"] if max_interval is None else max_interval)
    if max_iv < min_iv:
        max_iv = min_iv

    duration = float(clip_duration_seconds or 0.0)
    if duration <= 0:
        return [0.0]

    n = max(1, int(N))
    tolerance_seconds = duration * float(garbage_percent)
    raw_interval = tolerance_seconds / n
    snapshot_interval = max(min_iv, min(max_iv, raw_interval))
    num_snapshots = max(1, math.ceil(duration / snapshot_interval))

    timestamps: list[float] = []
    for i in range(num_snapshots):
        t = i * snapshot_interval
        if t > duration:
            break
        timestamps.append(round(min(t, duration), 3))

    if not timestamps:
        return [0.0]
    return timestamps


def compute_snapshot_interval(
    duration_seconds: float,
    *,
    garbage_percent: float | None = None,
    N: int | None = None,
) -> float:
    """Interval used by get_snapshot_timestamps (clamped)."""
    snap = _settings()
    gp = snap["garbage_percent"] if garbage_percent is None else float(garbage_percent)
    n = snap["resolution_factor"] if N is None else max(1, int(N))
    min_iv = float(snap["min_interval_sec"])
    max_iv = float(snap["max_interval_sec"])
    duration = float(duration_seconds or 0.0)
    if duration <= 0:
        return min_iv
    raw = (duration * gp) / n
    return round(max(min_iv, min(max_iv, raw)), 2)


def snapshot_times(duration_seconds: float, interval: float | None = None) -> list[float]:
    """Compatibility wrapper — prefers the tolerance formula over a fixed interval."""
    if interval is not None:
        duration = float(duration_seconds or 0.0)
        if duration <= 0:
            return [0.0]
        step = float(interval)
        num = max(1, math.ceil(duration / step))
        return [round(min(i * step, duration), 3) for i in range(num) if i * step <= duration] or [0.0]
    snap = _settings()
    return get_snapshot_timestamps(
        duration_seconds,
        garbage_percent=snap["garbage_percent"],
        N=snap["resolution_factor"],
    )


def snapshot_plan(source: Path, *, purpose: str = "clean") -> dict:
    source = source.expanduser().resolve()
    purpose = _normalize_purpose(purpose)
    if not source.exists():
        raise FileNotFoundError(source)
    try:
        duration = probe_media(source).duration or 0.0
    except (RuntimeError, OSError):
        duration = 0.0

    snap = _settings()
    if purpose == "label":
        interval = float(snap["label_preview_interval_sec"])
        span = float(snap["label_preview_span_sec"])
        times: list[float] = []
        t = 0.0
        while t < min(duration, span) - 0.05 and len(times) < _label_preview_count():
            times.append(round(t, 3))
            t += interval
        if not times:
            times = [0.0]
        garbage_hint = (
            f"Opening preview ({len(times)} shots) — use , . and ±3s to scrub through the clip"
        )
        max_garbage = 0
        n_factor = 0
        garbage_percent = 0.0
    else:
        garbage_percent = float(snap["garbage_percent"])
        n_factor = int(snap["resolution_factor"])
        interval = compute_snapshot_interval(duration, garbage_percent=garbage_percent, N=n_factor)
        times = get_snapshot_timestamps(duration, garbage_percent=garbage_percent, N=n_factor)
        max_garbage = round(duration * garbage_percent, 1) if duration else 0
        garbage_hint = (
            f"{n_factor}+ snapshots in a row without work "
            f"≈ garbage (up to ~{_format_minutes(max_garbage)} allowed · "
            f"interval {interval:.0f}s)"
        )

    return {
        "path": str(source),
        "purpose": purpose,
        "duration": duration,
        "interval_seconds": interval,
        "snapshot_count": len(times),
        "garbage_percent": garbage_percent,
        "resolution_factor": n_factor,
        "min_interval_sec": snap["min_interval_sec"],
        "max_interval_sec": snap["max_interval_sec"],
        "garbage_threshold": n_factor if purpose == "clean" else 0,
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


def _snapshot_video_filter() -> str:
    return f"scale={_snapshot_width()}:-2:flags=fast_bilinear,format=yuvj420p"


def _frame_looks_valid(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return path.stat().st_size >= 256
    except OSError:
        return False


def _input_strategies(source: Path, timestamp: float) -> list[list[str]]:
    source_arg = str(source)
    # -ss before -i: fast keyframe seek (critical on i5 / 16GB machines).
    keyframe = [
        "-skip_frame",
        "nokey",
        "-noaccurate_seek",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        source_arg,
    ]
    accurate = ["-ss", f"{timestamp:.3f}", "-i", source_arg]
    return [keyframe, accurate]


def _run_ffmpeg(command: list[str]) -> tuple[int, str]:
    global _current_ffmpeg_proc
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    with _lock:
        _current_ffmpeg_proc = proc
    try:
        _, stderr = proc.communicate(timeout=_ffmpeg_timeout())
        return proc.returncode if proc.returncode is not None else -1, stderr.strip()
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return -1, "ffmpeg timed out"
    finally:
        with _lock:
            if _current_ffmpeg_proc is proc:
                _current_ffmpeg_proc = None


def _kill_active_ffmpeg() -> None:
    global _current_ffmpeg_proc
    with _lock:
        proc = _current_ffmpeg_proc
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            proc.communicate(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


def extract_snapshot_jpeg(source: Path, timestamp: float, dest: Path) -> None:
    """Extract one JPEG thumbnail at ``timestamp`` (-ss before -i, -vframes 1)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()

    errors: list[str] = []
    vf = _snapshot_video_filter()
    qv = str(_jpeg_quality())
    for input_args in _input_strategies(source, timestamp):
        command = (
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-threads",
                str(SNAPSHOT_FFMPEG_THREADS),
            ]
            + input_args
            + [
                "-vframes",
                "1",
                "-an",
                "-vf",
                vf,
                "-pix_fmt",
                "yuvj420p",
                "-q:v",
                qv,
                str(dest),
            ]
        )
        code, detail = _run_ffmpeg(command)
        if code == 0 and _frame_looks_valid(dest):
            return
        if dest.exists():
            dest.unlink(missing_ok=True)
        errors.append(detail or "ffmpeg frame extract failed")

    raise RuntimeError(errors[-1] if errors else "ffmpeg frame extract failed")


def _extract_frame(source: Path, timestamp: float, dest: Path) -> None:
    extract_snapshot_jpeg(source, timestamp, dest)


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


def _ordered_times(times: list[float]) -> list[tuple[int, float]]:
    indexed = list(enumerate(times))
    if lite_mode_enabled():
        return indexed
    head = indexed[:BOOTSTRAP_FRAMES]
    tail = indexed[BOOTSTRAP_FRAMES:]
    return head + tail


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
        for index, timestamp in _ordered_times(times):
            if _job_cancelled(job_key):
                return

            dest = out_dir / f"frame_{index:05d}.jpg"
            try:
                _extract_frame(source, timestamp, dest)
            except RuntimeError:
                failures += 1
                continue

            frames.append({"index": index, "t": timestamp, "file": dest.name})
            frames.sort(key=lambda item: item["index"])
            pct = min(99, int((len(frames) / total) * 100))
            _update_job_progress(job_key, plan, frames, pct)

    min_required = 1 if purpose == "label" else (2 if lite_mode_enabled() else MIN_FRAMES_BEFORE_UI)
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


def _preempt_background_for_foreground(priority: int) -> None:
    if priority < PRIORITY_FOREGROUND:
        return
    global _active_job_key
    with _lock:
        if not _active_job_key:
            return
        active = _jobs.get(_active_job_key)
        if not active or active.get("priority", PRIORITY_BACKGROUND) >= PRIORITY_FOREGROUND:
            return
        active["status"] = "cancelled"
    _kill_active_ffmpeg()


def _remove_from_queue(key: str) -> None:
    if key not in _queue_keys:
        return
    rebuilt = deque(item for item in _queue if item[2] != key)
    _queue.clear()
    _queue.extend(rebuilt)
    _queue_keys.discard(key)


def _enqueue_snapshot(source: Path, purpose: str, *, priority: int) -> None:
    global _queue_worker_started
    key = _job_key(source, purpose)
    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") == "running" and key == _active_job_key:
            if priority > job.get("priority", PRIORITY_BACKGROUND):
                job["priority"] = priority
            return

        if job and job.get("status") == "ready":
            _jobs.pop(key, None)

        if key in _queue_keys:
            _remove_from_queue(key)

        entry = (-priority, time.time(), key, source, purpose)
        if priority >= PRIORITY_FOREGROUND:
            _queue.appendleft(entry)
        else:
            _queue.append(entry)
        _queue_keys.add(key)

        if job and job.get("status") in {"queued", "error", "cancelled"}:
            job["status"] = "queued"
            job["priority"] = max(job.get("priority", PRIORITY_BACKGROUND), priority)

        if not _queue_worker_started:
            _queue_worker_started = True
            threading.Thread(
                target=_queue_worker_loop,
                daemon=True,
                name="snapshot-queue",
            ).start()

    if priority >= PRIORITY_FOREGROUND:
        _preempt_background_for_foreground(priority)


def _queue_worker_loop() -> None:
    global _active_job_key
    while True:
        with _lock:
            if not _queue:
                item = None
            else:
                _, _, key, source, purpose = _queue.popleft()
                _queue_keys.discard(key)
                item = (key, source, purpose)

        if item is None:
            time.sleep(0.5 if lite_mode_enabled() else 0.25)
            continue

        key, source, purpose = item
        with _lock:
            job = _jobs.get(key)
            if job and job.get("status") in {"cancelled", "ready"}:
                continue

        try:
            plan = snapshot_plan(source, purpose=purpose)
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _jobs[key] = {"status": "error", "error": str(exc), "progress": 0}
            continue

        with _lock:
            job = _jobs.get(key)
            if job and job.get("status") in {"cancelled", "ready"}:
                continue
            priority = (job or {}).get("priority", PRIORITY_BACKGROUND)
            _jobs[key] = {
                "status": "running",
                "progress": 0,
                "plan": plan,
                "priority": priority,
            }
            _active_job_key = key

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
                    "priority": priority,
                }
        except Exception as exc:  # noqa: BLE001
            with _lock:
                if _jobs.get(key, {}).get("status") == "running":
                    _jobs[key] = {"status": "error", "error": str(exc), "progress": 0}
        finally:
            with _lock:
                if _active_job_key == key:
                    _active_job_key = None


def snapshot_config() -> dict:
    return performance_config()


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
            if job.get("status") == "queued":
                position = next(
                    (idx for idx, item in enumerate(_queue) if item[2] == key),
                    None,
                )
                result["queue_position"] = position
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
    return {"status": "queued", "progress": 0, "plan": plan, "priority": prio}


def cancel_snapshots(source: Path, *, purpose: str | None = None) -> None:
    source = source.expanduser().resolve()
    keys: list[str]
    if purpose is not None:
        keys = [_job_key(source, purpose)]
    else:
        keys = [_job_key(source, "clean"), _job_key(source, "label")]

    for key in keys:
        with _lock:
            job = _jobs.get(key)
            if job:
                job["status"] = "cancelled"
            _remove_from_queue(key)
            _jobs.pop(key, None)
        if key == _active_job_key:
            _kill_active_ffmpeg()


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

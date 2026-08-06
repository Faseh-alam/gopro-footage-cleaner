"""Adjacent MP4 annotation sidecars for contiguous work/garbage segments.

Sidecar naming: ``GX010001.segments.json`` next to ``GX010001.MP4``.
Segments must be ordered, contiguous from 0, non-overlapping, and cover
either a prefix or the full duration when the video is marked complete.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

END_EPSILON = 0.05  # seconds — treat near-end playhead as exact duration
MIN_SEGMENT = 0.05

_lock = threading.RLock()
_SIDECAR_SUFFIX = ".segments.json"


def sidecar_path_for(video: Path) -> Path:
    video = Path(video)
    return video.with_name(f"{video.stem}{_SIDECAR_SUFFIX}")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def normalize_boundary(t: float, duration: float | None) -> float:
    """Clamp to [0, duration] and snap near-end values to exact duration."""
    value = max(0.0, float(t))
    if duration is None or not (duration > 0):
        return value
    duration = float(duration)
    if value >= duration - END_EPSILON:
        return duration
    return min(value, duration)


def empty_annotation(
    *,
    source: str,
    duration: float | None = None,
    batch_name: str = "",
    factory: str = "",
    card_badge: str = "",
    device_type: str = "",
    device_id: str = "",
) -> dict:
    return {
        "version": 1,
        "source": source,
        "duration": float(duration) if duration is not None else None,
        "batch_name": batch_name,
        "factory": factory,
        "card_badge": card_badge,
        "device_type": device_type,
        "device_id": device_id,
        "segments": [],
        "updated_at": _now_iso(),
        "complete": False,
    }


def validate_segments(
    segments: list[dict],
    *,
    duration: float | None,
    require_complete: bool = False,
) -> tuple[bool, list[str], float]:
    """Return (ok, errors, covered_through)."""
    errors: list[str] = []
    if not isinstance(segments, list):
        return False, ["segments must be a list"], 0.0

    covered = 0.0
    for index, raw in enumerate(segments):
        if not isinstance(raw, dict):
            errors.append(f"segment {index}: must be an object")
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in {"work", "garbage"}:
            errors.append(f"segment {index}: kind must be work or garbage")
            continue
        try:
            start = float(raw.get("start", 0))
            end = float(raw.get("end", 0))
        except (TypeError, ValueError):
            errors.append(f"segment {index}: start/end must be numbers")
            continue
        if end <= start + MIN_SEGMENT:
            errors.append(f"segment {index}: end must be after start")
            continue
        if abs(start - covered) > END_EPSILON:
            errors.append(
                f"segment {index}: starts at {start:.3f} but coverage is at {covered:.3f}"
            )
        if kind == "work":
            task = str(raw.get("task") or "").strip()
            if not task:
                errors.append(f"segment {index}: work segment needs a task")
        covered = max(covered, end)

    if duration is not None and duration > 0 and covered > float(duration) + END_EPSILON:
        errors.append(f"coverage {covered:.3f}s exceeds duration {float(duration):.3f}s")

    if require_complete:
        if duration is None or duration <= 0:
            errors.append("duration required for completion")
        elif abs(covered - float(duration)) > END_EPSILON:
            errors.append(
                f"incomplete: covered {covered:.3f}s of {float(duration):.3f}s"
            )

    return not errors, errors, covered


def coverage_summary(annotation: dict) -> dict:
    segments = annotation.get("segments") or []
    duration = annotation.get("duration")
    try:
        duration_f = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_f = None

    work = 0.0
    garbage = 0.0
    tasks: dict[str, float] = {}
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", 0))
        except (TypeError, ValueError):
            continue
        length = max(0.0, end - start)
        kind = str(seg.get("kind") or "").lower()
        if kind == "work":
            work += length
            task = str(seg.get("task") or "").strip() or "unknown"
            tasks[task] = tasks.get(task, 0.0) + length
        elif kind == "garbage":
            garbage += length

    covered = work + garbage
    unreviewed = max(0.0, (duration_f or 0.0) - covered) if duration_f else 0.0
    ok, errors, _ = validate_segments(
        segments, duration=duration_f, require_complete=True
    )
    return {
        "duration": duration_f,
        "work_seconds": round(work, 3),
        "garbage_seconds": round(garbage, 3),
        "covered_seconds": round(covered, 3),
        "unreviewed_seconds": round(unreviewed, 3),
        "task_seconds": {k: round(v, 3) for k, v in sorted(tasks.items())},
        "segment_count": len(segments),
        "complete": bool(ok and duration_f),
        "errors": errors,
    }


def load_annotation(video: Path) -> dict | None:
    path = sidecar_path_for(video)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    data.setdefault("source", Path(video).name)
    data.setdefault("segments", [])
    data.setdefault("complete", False)
    return data


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(raw)
        temp_name = handle.name
    Path(temp_name).replace(path)


def save_annotation(video: Path, annotation: dict, *, require_complete: bool = False) -> dict:
    """Validate and write the sidecar. Returns the saved payload + summary."""
    video = Path(video).expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(f"Video not found: {video}")

    duration = annotation.get("duration")
    if duration is None:
        try:
            from .probe import probe_media

            duration = probe_media(video).duration
        except Exception:  # noqa: BLE001
            duration = None

    segments = []
    for raw in annotation.get("segments") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        try:
            start = float(raw.get("start", 0))
            end = float(raw.get("end", 0))
        except (TypeError, ValueError):
            continue
        start = normalize_boundary(start, duration)
        end = normalize_boundary(end, duration)
        item = {
            "id": str(raw.get("id") or uuid.uuid4()),
            "kind": kind,
            "start": round(start, 3),
            "end": round(end, 3),
        }
        if kind == "work":
            item["task"] = str(raw.get("task") or "").strip()
        segments.append(item)

    ok, errors, _ = validate_segments(
        segments, duration=float(duration) if duration is not None else None, require_complete=require_complete
    )
    if not ok and require_complete:
        raise ValueError("; ".join(errors))
    # Soft validation always for structural errors (overlaps/gaps while saving partial)
    soft_ok, soft_errors, _ = validate_segments(
        segments,
        duration=float(duration) if duration is not None else None,
        require_complete=False,
    )
    if not soft_ok:
        # Allow empty / incomplete prefix only — reject true gaps/overlaps
        gap_or_overlap = [
            e
            for e in soft_errors
            if "starts at" in e or "exceeds duration" in e or "end must be after" in e
        ]
        if gap_or_overlap:
            raise ValueError("; ".join(gap_or_overlap))

    payload = {
        "version": 1,
        "source": video.name,
        "size_bytes": video.stat().st_size,
        "mtime_ns": video.stat().st_mtime_ns,
        "duration": float(duration) if duration is not None else None,
        "batch_name": str(annotation.get("batch_name") or ""),
        "factory": str(annotation.get("factory") or ""),
        "card_badge": str(annotation.get("card_badge") or ""),
        "device_type": str(annotation.get("device_type") or ""),
        "device_id": str(annotation.get("device_id") or ""),
        "segments": segments,
        "updated_at": _now_iso(),
    }
    summary = coverage_summary(payload)
    payload["complete"] = summary["complete"]

    with _lock:
        _atomic_write(sidecar_path_for(video), payload)
        # Human-readable companion
        txt = sidecar_path_for(video).with_suffix("").with_suffix(".segments.txt")
        lines = [
            f"source: {payload['source']}",
            f"duration: {payload.get('duration')}",
            f"batch: {payload.get('batch_name')}",
            f"factory: {payload.get('factory')}",
            f"card: {payload.get('card_badge')}",
            f"device: {payload.get('device_type')} / {payload.get('device_id')}",
            f"complete: {payload['complete']}",
            "",
        ]
        for seg in segments:
            if seg["kind"] == "work":
                lines.append(f"{seg['start']:.3f}-{seg['end']:.3f}  WORK  {seg.get('task', '')}")
            else:
                lines.append(f"{seg['start']:.3f}-{seg['end']:.3f}  GARBAGE")
        try:
            txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    return {"annotation": payload, "summary": summary}


def append_segment(
    video: Path,
    *,
    kind: str,
    end: float,
    task: str = "",
    context: dict | None = None,
) -> dict:
    """Append a contiguous segment from current coverage to ``end``."""
    video = Path(video).expanduser().resolve()
    existing = load_annotation(video) or empty_annotation(source=video.name)
    if context:
        for key in ("batch_name", "factory", "card_badge", "device_type", "device_id", "duration"):
            if context.get(key) not in (None, ""):
                existing[key] = context[key]

    duration = existing.get("duration")
    if duration is None:
        try:
            from .probe import probe_media

            duration = probe_media(video).duration
            existing["duration"] = duration
        except Exception:  # noqa: BLE001
            pass

    end = normalize_boundary(end, duration)
    _, _, covered = validate_segments(
        existing.get("segments") or [],
        duration=float(duration) if duration is not None else None,
        require_complete=False,
    )
    start = covered
    if end <= start + MIN_SEGMENT:
        raise ValueError("Playhead has not advanced enough past the current anchor")

    kind = kind.strip().lower()
    if kind not in {"work", "garbage"}:
        raise ValueError("kind must be work or garbage")
    segment = {"id": str(uuid.uuid4()), "kind": kind, "start": start, "end": end}
    if kind == "work":
        task = task.strip()
        if not task:
            raise ValueError("task is required for work segments")
        segment["task"] = task

    segments = list(existing.get("segments") or [])
    segments.append(segment)
    existing["segments"] = segments
    return save_annotation(video, existing)


def undo_last_segment(video: Path) -> dict:
    video = Path(video).expanduser().resolve()
    existing = load_annotation(video)
    if not existing or not existing.get("segments"):
        raise ValueError("No segments to undo")
    existing["segments"] = list(existing["segments"])[:-1]
    return save_annotation(video, existing)


def find_sidecars(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(root.rglob(f"*{_SIDECAR_SUFFIX}"))


_SAFE_BATCH = re.compile(r"[^\w.\-]+")


def safe_batch_id(name: str) -> str:
    slug = _SAFE_BATCH.sub("_", (name or "").strip()).strip("._")
    return slug or "batch"

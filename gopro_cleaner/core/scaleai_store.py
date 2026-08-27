"""Layered ScaleAI sidecars for parent-task cycles and linked subtasks.

Unlike the normal contiguous ``*.segments.json`` annotation, ScaleAI has two
overlapping layers:

1. clean repetitions of the parent task (e.g. one complete label attachment);
2. micro-subtasks inside each repetition (e.g. grab-cloth, place-label).

The separate ``*.scaleai.json`` file keeps both layers without modifying the
normal review sidecar or the source video.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .annotation_store import MIN_SEGMENT, normalize_boundary, resolve_media_duration

VERSION = 1
SIDECAR_SUFFIX = ".scaleai.json"
EPSILON = 0.001

_lock = threading.RLock()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sidecar_path_for(video: Path) -> Path:
    path = Path(video)
    return path.with_name(f"{path.stem}{SIDECAR_SUFFIX}")


SOURCE_BUCKET_NAMES = {"aws", "google drive"}


def infer_parent_task(video: Path, root: Path | None = None) -> str:
    """Infer parent task from the real ScaleAI delivery layout.

    Expected paths look like::

        …/50 hours/Google Drive/<task>/<task>/clip.mp4
        …/50 hours/AWS/<task>/<task>/clip.mp4

    The parent task is the first folder under AWS / Google Drive, even when that
    task name is nested twice before the footage files.
    """
    source = Path(video).expanduser().resolve()

    for ancestor in source.parents:
        if ancestor.name.lower() not in SOURCE_BUCKET_NAMES:
            continue
        try:
            relative = source.relative_to(ancestor)
        except ValueError:
            continue
        # relative = <task> / [optional nested folders…] / filename
        if len(relative.parts) >= 2:
            return relative.parts[0].strip()

    if root is not None:
        resolved_root = Path(root).expanduser().resolve()
        try:
            relative = source.relative_to(resolved_root)
        except ValueError:
            relative = None
        if relative is not None:
            parts = [part for part in relative.parts[:-1] if part.strip()]
            # Drop delivery wrappers so we land on the task folder.
            while parts and parts[0].lower() in SOURCE_BUCKET_NAMES | {"50 hours"}:
                parts = parts[1:]
            if parts:
                return parts[0].strip()

    return source.parent.name.strip() or "Uncategorized"


def empty_annotation(
    video: Path,
    *,
    parent_task: str | None = None,
    root: Path | None = None,
) -> dict:
    source = Path(video).expanduser().resolve()
    duration = resolve_media_duration(source)
    return {
        "version": VERSION,
        "source": str(source),
        "duration": duration,
        "parent_task": (parent_task or infer_parent_task(source, root)).strip(),
        "parent_cycles": [],
        "example_cycle_id": None,
        "subtask_names": [],
        "subtask_segments": [],
        "updated_at": _now_iso(),
    }


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".tmp",
        prefix=f".{path.name}.",
        dir=str(path.parent),
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        temp = Path(handle.name)
    temp.replace(path)


def _normalize(payload: dict, video: Path) -> dict:
    source = Path(video).expanduser().resolve()
    duration = resolve_media_duration(source, payload.get("duration"))
    parent_task = str(payload.get("parent_task") or infer_parent_task(source)).strip()

    cycles: list[dict] = []
    seen_cycle_ids: set[str] = set()
    for raw in payload.get("parent_cycles") or []:
        try:
            start = normalize_boundary(float(raw.get("start", 0)), duration)
            end = normalize_boundary(float(raw.get("end", 0)), duration)
        except (TypeError, ValueError):
            continue
        if end <= start + MIN_SEGMENT:
            continue
        cycle_id = str(raw.get("id") or uuid.uuid4().hex)
        if cycle_id in seen_cycle_ids:
            cycle_id = uuid.uuid4().hex
        seen_cycle_ids.add(cycle_id)
        cycles.append(
            {
                "id": cycle_id,
                "start": round(start, 6),
                "end": round(end, 6),
                "created_at": str(raw.get("created_at") or _now_iso()),
            }
        )
    cycles.sort(key=lambda row: (row["start"], row["end"]))

    # Parent cycles may have garbage gaps, but clean cycles cannot overlap.
    previous_end = -1.0
    for cycle in cycles:
        if cycle["start"] < previous_end - EPSILON:
            raise ValueError("Parent task cycles cannot overlap")
        previous_end = cycle["end"]

    cycle_by_id = {row["id"]: row for row in cycles}
    names: list[str] = []
    seen_names: set[str] = set()
    for raw in payload.get("subtask_names") or []:
        name = str(raw).strip()
        key = name.lower()
        if name and key not in seen_names:
            names.append(name)
            seen_names.add(key)

    segments: list[dict] = []
    by_cycle: dict[str, list[dict]] = {}
    for raw in payload.get("subtask_segments") or []:
        cycle_id = str(raw.get("parent_cycle_id") or "").strip()
        cycle = cycle_by_id.get(cycle_id)
        task = str(raw.get("task") or "").strip()
        if not cycle or not task:
            continue
        try:
            start = float(raw.get("start", 0))
            end = float(raw.get("end", 0))
        except (TypeError, ValueError):
            continue
        if end <= start + MIN_SEGMENT:
            continue
        if start < cycle["start"] - EPSILON or end > cycle["end"] + EPSILON:
            raise ValueError(
                f"Subtask {task!r} must stay inside parent cycle {cycle_id}"
            )
        row = {
            "id": str(raw.get("id") or uuid.uuid4().hex),
            "parent_cycle_id": cycle_id,
            "start": round(start, 6),
            "end": round(end, 6),
            "task": task,
            "created_at": str(raw.get("created_at") or _now_iso()),
        }
        by_cycle.setdefault(cycle_id, []).append(row)
        if task.lower() not in seen_names:
            names.append(task)
            seen_names.add(task.lower())

    # A hand can only be doing one declared micro-subtask at a time in a cycle.
    for cycle_id, rows in by_cycle.items():
        rows.sort(key=lambda row: (row["start"], row["end"]))
        previous_end = cycle_by_id[cycle_id]["start"]
        for row in rows:
            if row["start"] < previous_end - EPSILON:
                raise ValueError(f"Subtasks overlap inside parent cycle {cycle_id}")
            previous_end = row["end"]
        segments.extend(rows)
    segments.sort(key=lambda row: (row["start"], row["end"]))

    example_id = str(payload.get("example_cycle_id") or "").strip() or None
    if example_id not in cycle_by_id:
        example_id = None

    return {
        "version": VERSION,
        "source": str(source),
        "duration": duration,
        "parent_task": parent_task,
        "parent_cycles": cycles,
        "example_cycle_id": example_id,
        "subtask_names": names,
        "subtask_segments": segments,
        "updated_at": _now_iso(),
    }


def load_annotation(
    video: Path,
    *,
    parent_task: str | None = None,
    root: Path | None = None,
) -> dict:
    source = Path(video).expanduser().resolve()
    path = sidecar_path_for(source)
    with _lock:
        if not path.is_file():
            normalized = empty_annotation(source, parent_task=parent_task, root=root)
        else:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise ValueError(f"Could not read ScaleAI sidecar: {exc}") from exc
            if not isinstance(raw, dict):
                raise ValueError("ScaleAI sidecar must contain an object")
            normalized = _normalize(raw, source)
        # Vocabulary is shared by the parent-task folder. Merge names from
        # sibling videos so the CEO-confirmed list follows the labeler across
        # every source without introducing another database/service.
        known = {name.lower() for name in normalized["subtask_names"]}
        parent_example = None
        if normalized.get("example_cycle_id"):
            parent_example = {
                "source": str(source),
                "cycle_id": normalized["example_cycle_id"],
            }
        for sibling in source.parent.glob(f"*{SIDECAR_SUFFIX}"):
            if sibling == path:
                continue
            try:
                sibling_raw = json.loads(sibling.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if str(sibling_raw.get("parent_task") or "").strip() != normalized["parent_task"]:
                continue
            sibling_example = str(sibling_raw.get("example_cycle_id") or "").strip()
            if sibling_example and any(
                str(cycle.get("id") or "") == sibling_example
                for cycle in sibling_raw.get("parent_cycles") or []
            ):
                sibling_source = source_for_sidecar(sibling)
                parent_example = {
                    "source": str(sibling_source) if sibling_source else str(sibling),
                    "cycle_id": sibling_example,
                }
            for raw_name in sibling_raw.get("subtask_names") or []:
                name = str(raw_name).strip()
                if name and name.lower() not in known:
                    normalized["subtask_names"].append(name)
                    known.add(name.lower())
        normalized["parent_example"] = parent_example
        return normalized


def save_annotation(video: Path, payload: dict) -> dict:
    source = Path(video).expanduser().resolve(strict=True)
    with _lock:
        normalized = _normalize(payload, source)
        _atomic_write(sidecar_path_for(source), normalized)
        return normalized


def add_parent_cycle(
    video: Path,
    start: float,
    end: float,
    *,
    parent_task: str | None = None,
    root: Path | None = None,
) -> dict:
    with _lock:
        payload = load_annotation(video, parent_task=parent_task, root=root)
        payload["parent_cycles"].append(
            {
                "id": uuid.uuid4().hex,
                "start": float(start),
                "end": float(end),
                "created_at": _now_iso(),
            }
        )
        return save_annotation(video, payload)


def delete_parent_cycle(video: Path, cycle_id: str) -> dict:
    with _lock:
        payload = load_annotation(video)
        before = len(payload["parent_cycles"])
        payload["parent_cycles"] = [
            row for row in payload["parent_cycles"] if row["id"] != cycle_id
        ]
        if len(payload["parent_cycles"]) == before:
            raise ValueError("Parent cycle not found")
        payload["subtask_segments"] = [
            row
            for row in payload["subtask_segments"]
            if row["parent_cycle_id"] != cycle_id
        ]
        if payload.get("example_cycle_id") == cycle_id:
            payload["example_cycle_id"] = None
        return save_annotation(video, payload)


def select_example(video: Path, cycle_id: str) -> dict:
    with _lock:
        payload = load_annotation(video)
        if not any(row["id"] == cycle_id for row in payload["parent_cycles"]):
            raise ValueError("Parent cycle not found")
        for sibling in Path(video).expanduser().resolve().parent.glob(
            f"*{SIDECAR_SUFFIX}"
        ):
            if sibling == sidecar_path_for(Path(video)):
                continue
            try:
                raw = json.loads(sibling.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if (
                isinstance(raw, dict)
                and str(raw.get("parent_task") or "").strip() == payload["parent_task"]
                and raw.get("example_cycle_id")
            ):
                raw["example_cycle_id"] = None
                raw["updated_at"] = _now_iso()
                _atomic_write(sibling, raw)
        payload["example_cycle_id"] = cycle_id
        save_annotation(video, payload)
        return load_annotation(video)


def set_subtask_names(video: Path, names: list[str]) -> dict:
    with _lock:
        payload = load_annotation(video)
        payload["subtask_names"] = names
        return save_annotation(video, payload)


def add_subtask_segment(
    video: Path,
    cycle_id: str,
    task: str,
    start: float,
    end: float,
) -> dict:
    with _lock:
        payload = load_annotation(video)
        payload["subtask_segments"].append(
            {
                "id": uuid.uuid4().hex,
                "parent_cycle_id": cycle_id,
                "task": task,
                "start": float(start),
                "end": float(end),
                "created_at": _now_iso(),
            }
        )
        return save_annotation(video, payload)


def delete_subtask_segment(video: Path, segment_id: str) -> dict:
    with _lock:
        payload = load_annotation(video)
        before = len(payload["subtask_segments"])
        payload["subtask_segments"] = [
            row for row in payload["subtask_segments"] if row["id"] != segment_id
        ]
        if len(payload["subtask_segments"]) == before:
            raise ValueError("Subtask segment not found")
        return save_annotation(video, payload)


def cycle_by_id(payload: dict, cycle_id: str) -> dict:
    for cycle in payload.get("parent_cycles") or []:
        if cycle.get("id") == cycle_id:
            return cycle
    raise ValueError("Parent cycle not found")


def iter_sidecars(root: Path) -> list[Path]:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        return []
    return sorted(base.rglob(f"*{SIDECAR_SUFFIX}"))


def source_for_sidecar(sidecar: Path) -> Path | None:
    name = sidecar.name
    if not name.endswith(SIDECAR_SUFFIX):
        return None
    stem = name[: -len(SIDECAR_SUFFIX)]
    for suffix in (".MP4", ".mp4", ".MOV", ".mov"):
        candidate = sidecar.with_name(f"{stem}{suffix}")
        if candidate.is_file():
            return candidate.resolve()
    return None

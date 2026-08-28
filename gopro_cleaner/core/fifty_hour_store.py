"""Task-level JSON storage for the Scale AI 50-hour workflow.

Each main-task folder owns one ``segment.json`` containing source metadata and
timestamp cuts, plus one ``manifest.json`` containing stable subtask IDs and
their generated short clips. Free-form subtask / garbage gaps are allowed.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .annotation_store import MIN_SEGMENT, normalize_boundary, resolve_media_duration

VERSION = 2
SEGMENT_FILE = "segment.json"
MANIFEST_FILE = "manifest.json"
SIDECAR_SUFFIX = ".json"
LABELING_DIR = "_labeling"
TASKS_FILE = "tasks.json"
PROGRESS_FILE = "progress.json"
EPSILON = 0.001
CL_RE = re.compile(r"(?i)\b(C\d{3,6}|CL[-_]?\w+)\b")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
CLIP_NAME_RE = re.compile(
    r"^(?P<camera>[A-Za-z0-9]+)-(?P<subtask>\d{3})-(?P<clip>\d{3})\.(?:mp4|mov)$",
    re.IGNORECASE,
)
SOURCE_CLIP_NAME_RE = re.compile(
    r"^(?P<source>[A-Za-z0-9_-]+)\.(?P<subtask>\d{3})\.(?P<clip>\d{3})\.(?:mp4|mov)$",
    re.IGNORECASE,
)

_lock = threading.RLock()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _atomic_write(path: Path, payload: dict | list) -> None:
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


def _read_json(path: Path) -> dict | list:
    """Read sidecars from Windows tools that may have saved CP-1252 JSON."""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    return json.loads(text)


def safe_label_name(label: str) -> str:
    cleaned = SAFE_NAME_RE.sub("-", (label or "").strip()).strip("-._")
    return cleaned or "untitled"


def task_directory(video: Path) -> Path:
    """Folder containing the source videos and task-level JSON files."""
    return Path(video).expanduser().resolve().parent


def sidecar_path_for(video: Path) -> Path:
    """Task-level segment document shared by every source video."""
    return task_directory(video) / SEGMENT_FILE


def manifest_path_for(video: Path) -> Path:
    return task_directory(video) / MANIFEST_FILE


def labeling_dir(root: Path) -> Path:
    return Path(root).expanduser().resolve() / LABELING_DIR


def tasks_path(root: Path) -> Path:
    return labeling_dir(root) / TASKS_FILE


def progress_path(root: Path) -> Path:
    return labeling_dir(root) / PROGRESS_FILE


def infer_parent_task(video: Path, root: Path | None = None) -> str:
    """Parent task = first folder under the opened 50-hour root."""
    source = Path(video).expanduser().resolve()
    if root is not None:
        resolved_root = Path(root).expanduser().resolve()
        try:
            relative = source.relative_to(resolved_root)
        except ValueError:
            relative = None
        if relative is not None:
            parts = [
                part
                for part in relative.parts[:-1]
                if part.strip() and part.lower() != LABELING_DIR
            ]
            # Drop known delivery wrappers if present.
            while parts and parts[0].lower() in {
                "aws",
                "google drive",
                "50 hours",
                "50-hour",
                "50-hours",
            }:
                parts = parts[1:]
            if parts:
                return parts[0].strip()
    return source.parent.name.strip() or "Uncategorized"


def _infer_cl_number(video: Path, meta: dict) -> str | None:
    for part in Path(video).parts:
        match = CL_RE.search(part)
        if match:
            return match.group(1).upper().replace("_", "-")
    media_uid = meta.get("media_uid")
    if media_uid:
        return str(media_uid)
    return None


def _media_fields(video: Path) -> dict:
    meta: dict = {}
    try:
        from .gopro_meta import get_media_meta

        meta = get_media_meta(Path(video)) or {}
    except Exception:  # noqa: BLE001
        meta = {}
    return {
        "camera_serial": meta.get("camera_serial") or None,
        "cl_number": _infer_cl_number(video, meta),
        "media_meta": meta,
    }


def empty_annotation(
    video: Path,
    *,
    parent_task: str | None = None,
    root: Path | None = None,
) -> dict:
    source = Path(video).expanduser().resolve()
    duration = resolve_media_duration(source)
    fields = _media_fields(source)
    return {
        "version": VERSION,
        "source_video": source.name,
        "source_path": str(source),
        "parent_task": (parent_task or infer_parent_task(source, root)).strip(),
        "camera_serial": fields["camera_serial"],
        "cl_number": fields["cl_number"],
        "duration_seconds": duration,
        "media_meta": fields["media_meta"] or {},
        "segments": [],
        "updated_at": _now_iso(),
    }


def _empty_segment_doc(parent_task: str) -> dict:
    return {
        "version": VERSION,
        "main_task": parent_task,
        "videos": [],
        "updated_at": _now_iso(),
    }


def _read_segment_doc(path: Path, parent_task: str) -> dict:
    if not path.is_file():
        return _empty_segment_doc(parent_task)
    raw = _read_json(path)
    if not isinstance(raw, dict):
        return _empty_segment_doc(parent_task)
    # Compatibility with the old adjacent VIDEO.json shape.
    if isinstance(raw.get("segments"), list):
        return {
            "version": VERSION,
            "main_task": str(raw.get("parent_task") or parent_task),
            "videos": [raw],
            "updated_at": str(raw.get("updated_at") or _now_iso()),
        }
    videos = raw.get("videos")
    if not isinstance(videos, list):
        videos = []
    return {
        "version": VERSION,
        "main_task": str(raw.get("main_task") or raw.get("parent_task") or parent_task),
        "videos": [row for row in videos if isinstance(row, dict)],
        "updated_at": str(raw.get("updated_at") or _now_iso()),
    }


def normalize_annotation(raw: dict, video: Path, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    base = empty_annotation(source, root=root)
    if not isinstance(raw, dict):
        return base

    duration = resolve_media_duration(
        source, raw.get("duration_seconds", raw.get("duration"))
    )
    segments: list[dict] = []
    for index, item in enumerate(raw.get("segments") or [], start=1):
        if not isinstance(item, dict):
            continue
        try:
            start = normalize_boundary(float(item["start"]), duration)
            end = normalize_boundary(float(item["end"]), duration)
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < MIN_SEGMENT - EPSILON:
            continue
        seg_type = str(item.get("type") or item.get("kind") or "subtask").strip().lower()
        if seg_type in {"work", "task"}:
            seg_type = "subtask"
        if seg_type not in {"subtask", "garbage"}:
            seg_type = "subtask"
        label = str(item.get("label") or item.get("task") or "").strip()
        if seg_type == "garbage":
            label = "garbage"
        elif not label:
            continue
        segment = {
            "id": item.get("id") or index,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(end - start, 3),
            "type": seg_type,
            "label": label,
        }
        for key in (
            "subtask_id",
            "clip_filename",
            "clip_path",
            "clip_serial",
            "camera_serial",
        ):
            if item.get(key) not in (None, ""):
                segment[key] = item[key]
        segments.append(segment)
    segments.sort(key=lambda s: (float(s["start"]), float(s["end"])))

    parent_task = str(raw.get("parent_task") or "").strip() or base["parent_task"]
    camera_serial = raw.get("camera_serial")
    if camera_serial in (None, ""):
        camera_serial = base["camera_serial"]
    cl_number = raw.get("cl_number")
    if cl_number in (None, ""):
        cl_number = base["cl_number"]

    return {
        "version": int(raw.get("version") or VERSION),
        # Always heal copied annotations to the source's current computer/path.
        "source_video": source.name,
        "source_path": str(source),
        "parent_task": parent_task,
        "camera_serial": camera_serial,
        "cl_number": cl_number,
        "duration_seconds": duration,
        "media_meta": raw.get("media_meta")
        if isinstance(raw.get("media_meta"), dict)
        else base.get("media_meta") or {},
        "segments": segments,
        "updated_at": str(raw.get("updated_at") or _now_iso()),
    }


def load_annotation(video: Path, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    sidecar = sidecar_path_for(source)
    with _lock:
        if not sidecar.is_file():
            # Read an old per-video sidecar if this task has not been migrated yet.
            legacy = source.with_name(f"{source.stem}.json")
            if legacy.is_file() and legacy != sidecar:
                annotation = normalize_annotation(_read_json(legacy), source, root=root)
                _ensure_manifest_from_annotation(source, annotation)
                save_annotation(source, annotation, root=root)
                return annotation
            return empty_annotation(source, root=root)
        parent = infer_parent_task(source, root)
        doc = _read_segment_doc(sidecar, parent)
        source_key = str(source).lower()
        raw = next(
            (
                row
                for row in doc["videos"]
                if str(row.get("source_path") or "").lower() == source_key
                or (
                    not row.get("source_path")
                    and str(row.get("source_video") or "").lower() == source.name.lower()
                )
            ),
            None,
        )
        annotation = normalize_annotation(raw or {}, source, root=root)
        _ensure_manifest_from_annotation(source, annotation)
        # Manifest loading may migrate clip files into subtask folders and update
        # clip references in segment.json. Return that migrated annotation.
        refreshed = _read_segment_doc(sidecar, parent)
        refreshed_raw = next(
            (
                row
                for row in refreshed["videos"]
                if str(row.get("source_path") or "").lower() == source_key
            ),
            raw,
        )
        return normalize_annotation(refreshed_raw or {}, source, root=root)


def save_annotation(video: Path, annotation: dict, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    normalized = normalize_annotation(annotation, source, root=root)
    normalized["updated_at"] = _now_iso()
    with _lock:
        path = sidecar_path_for(source)
        doc = _read_segment_doc(path, normalized["parent_task"])
        source_key = str(source).lower()
        videos = [
            row
            for row in doc["videos"]
            if str(row.get("source_path") or "").lower() != source_key
            and not (
                not row.get("source_path")
                and str(row.get("source_video") or "").lower() == source.name.lower()
            )
        ]
        videos.append(normalized)
        videos.sort(key=lambda row: str(row.get("source_video") or "").lower())
        doc.update(
            {
                "version": VERSION,
                "main_task": normalized["parent_task"],
                "videos": videos,
                "updated_at": _now_iso(),
            }
        )
        _atomic_write(path, doc)
    if root is not None:
        refresh_progress(Path(root))
    return normalized


def remove_video_annotation(video: Path) -> None:
    """Remove one source entry without deleting the shared task segment.json."""
    source = Path(video).expanduser().resolve()
    path = sidecar_path_for(source)
    with _lock:
        if not path.is_file():
            return
        doc = _read_segment_doc(path, source.parent.name)
        source_key = str(source).lower()
        doc["videos"] = [
            row
            for row in doc["videos"]
            if str(row.get("source_path") or "").lower() != source_key
            and str(row.get("source_video") or "").lower() != source.name.lower()
        ]
        doc["updated_at"] = _now_iso()
        _atomic_write(path, doc)


def _next_segment_id(annotation: dict) -> int:
    used = []
    for segment in annotation.get("segments") or []:
        try:
            used.append(int(segment.get("id")))
        except (TypeError, ValueError):
            continue
    return (max(used) + 1) if used else 1


BOUNDARY_GAP = 0.01


def _resolve_non_overlapping_start(
    start: float,
    end: float,
    existing_segments: list[dict],
    *,
    ignore_id: str | None = None,
) -> float:
    """Bump ``start`` by 0.01s when it overlaps or shares an end boundary.

    Only previous-segment end collisions are adjusted — never bump because our
    end touches a later segment's start.
    """
    start_n = float(start)
    end_n = float(end)
    for _ in range(max(8, len(existing_segments) + 2)):
        conflict_end: float | None = None
        for existing in existing_segments:
            if ignore_id is not None and str(existing.get("id")) == ignore_id:
                continue
            es = float(existing["start"])
            ee = float(existing["end"])
            overlaps = max(start_n, es) < min(end_n, ee) - EPSILON
            shares_end = abs(start_n - ee) <= EPSILON
            if overlaps or shares_end:
                conflict_end = ee if conflict_end is None else max(conflict_end, ee)
        if conflict_end is None:
            return round(start_n, 3)
        start_n = round(conflict_end + BOUNDARY_GAP, 3)
        if end_n - start_n < MIN_SEGMENT - EPSILON:
            raise ValueError(
                f"Too close to a previous mark — move playhead further "
                f"(need ≥{MIN_SEGMENT:.2f}s after a +{BOUNDARY_GAP:.2f}s gap)"
            )
    raise ValueError("Segment overlaps an existing marking")


def add_segment(
    video: Path,
    *,
    start: float,
    end: float,
    label: str,
    segment_type: str = "subtask",
    root: Path | None = None,
) -> dict:
    source = Path(video).expanduser().resolve()
    with _lock:
        annotation = load_annotation(source, root=root)
        duration = annotation.get("duration_seconds")
        start_n = normalize_boundary(float(start), duration)
        end_n = normalize_boundary(float(end), duration)
        if end_n - start_n < MIN_SEGMENT - EPSILON:
            raise ValueError(f"Segment too short (min {MIN_SEGMENT}s)")
        seg_type = str(segment_type or "subtask").strip().lower()
        if seg_type in {"work", "task"}:
            seg_type = "subtask"
        if seg_type not in {"subtask", "garbage"}:
            raise ValueError("type must be subtask or garbage")
        clean_label = "garbage" if seg_type == "garbage" else str(label or "").strip()
        if seg_type == "subtask" and not clean_label:
            raise ValueError("label is required for subtask segments")
        start_n = _resolve_non_overlapping_start(
            start_n, end_n, list(annotation.get("segments") or [])
        )
        if end_n - start_n < MIN_SEGMENT - EPSILON:
            raise ValueError(f"Segment too short (min {MIN_SEGMENT}s)")
        segment = {
            "id": _next_segment_id(annotation),
            "start": round(start_n, 3),
            "end": round(end_n, 3),
            "duration": round(end_n - start_n, 3),
            "type": seg_type,
            "label": clean_label,
        }
        annotation["segments"] = list(annotation.get("segments") or []) + [segment]
        annotation["segments"].sort(key=lambda s: (float(s["start"]), float(s["end"])))
        annotation["updated_at"] = _now_iso()
        save_annotation(source, annotation, root=root)
    if root is not None:
        if seg_type == "subtask":
            add_label(root, annotation["parent_task"], clean_label)
        refresh_progress(Path(root))
    elif seg_type == "subtask":
        # Still try to update vocab when root unknown (parent folder of parent task).
        pass
    return annotation


def delete_segment(video: Path, segment_id: str | int, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    target = str(segment_id)
    with _lock:
        annotation = load_annotation(source, root=root)
        before = len(annotation.get("segments") or [])
        annotation["segments"] = [
            segment
            for segment in annotation.get("segments") or []
            if str(segment.get("id")) != target
        ]
        if len(annotation["segments"]) == before:
            raise ValueError(f"Segment not found: {segment_id}")
        annotation["updated_at"] = _now_iso()
        save_annotation(source, annotation, root=root)
    if root is not None:
        refresh_progress(Path(root))
    return annotation


def update_segment(
    video: Path,
    segment_id: str | int,
    *,
    start: float | None = None,
    end: float | None = None,
    label: str | None = None,
    segment_type: str | None = None,
    root: Path | None = None,
) -> dict:
    source = Path(video).expanduser().resolve()
    target = str(segment_id)
    with _lock:
        annotation = load_annotation(source, root=root)
        duration = annotation.get("duration_seconds")
        found = None
        for segment in annotation.get("segments") or []:
            if str(segment.get("id")) == target:
                found = segment
                break
        if found is None:
            raise ValueError(f"Segment not found: {segment_id}")
        new_start = normalize_boundary(
            float(start) if start is not None else float(found["start"]), duration
        )
        new_end = normalize_boundary(
            float(end) if end is not None else float(found["end"]), duration
        )
        if new_end - new_start < MIN_SEGMENT - EPSILON:
            raise ValueError(f"Segment too short (min {MIN_SEGMENT}s)")
        new_type = str(segment_type or found.get("type") or "subtask").strip().lower()
        if new_type in {"work", "task"}:
            new_type = "subtask"
        if new_type not in {"subtask", "garbage"}:
            raise ValueError("type must be subtask or garbage")
        new_label = (
            "garbage"
            if new_type == "garbage"
            else str(label if label is not None else found.get("label") or "").strip()
        )
        if new_type == "subtask" and not new_label:
            raise ValueError("label is required for subtask segments")
        new_start = _resolve_non_overlapping_start(
            new_start,
            new_end,
            list(annotation.get("segments") or []),
            ignore_id=target,
        )
        if new_end - new_start < MIN_SEGMENT - EPSILON:
            raise ValueError(f"Segment too short (min {MIN_SEGMENT}s)")
        found["start"] = round(new_start, 3)
        found["end"] = round(new_end, 3)
        found["duration"] = round(new_end - new_start, 3)
        found["type"] = new_type
        found["label"] = new_label
        annotation["segments"].sort(key=lambda s: (float(s["start"]), float(s["end"])))
        annotation["updated_at"] = _now_iso()
        save_annotation(source, annotation, root=root)
    if root is not None:
        if new_type == "subtask":
            add_label(root, annotation["parent_task"], new_label)
        refresh_progress(Path(root))
    return annotation


def undo_last_segment(video: Path, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    with _lock:
        annotation = load_annotation(source, root=root)
        segments = list(annotation.get("segments") or [])
        if not segments:
            raise ValueError("No segments to undo")
        segments.pop()
        annotation["segments"] = segments
        annotation["updated_at"] = _now_iso()
        save_annotation(source, annotation, root=root)
    if root is not None:
        refresh_progress(Path(root))
    return annotation


def iter_sidecars(root: Path):
    root = Path(root).expanduser().resolve()
    yield from sorted(root.rglob(SEGMENT_FILE))


def source_for_sidecar(sidecar: Path) -> Path | None:
    """Return the first source in a task-level segment file (legacy helper)."""
    sidecar = Path(sidecar).expanduser().resolve()
    try:
        raw = _read_json(sidecar)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    rows = raw.get("videos") if isinstance(raw, dict) else None
    if isinstance(rows, list):
        for row in rows:
            candidate = Path(str(row.get("source_path") or "")).expanduser()
            if candidate.is_file():
                return candidate.resolve()
    return None


def annotated_sources(root: Path) -> list[Path]:
    sources: list[Path] = []
    seen: set[str] = set()
    for sidecar in iter_sidecars(root):
        try:
            raw = _read_json(sidecar)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        for row in raw.get("videos") or [] if isinstance(raw, dict) else []:
            candidate = Path(str(row.get("source_path") or "")).expanduser()
            if not candidate.is_file():
                candidate = sidecar.parent / str(row.get("source_video") or "")
            if candidate.is_file() and str(candidate.resolve()).lower() not in seen:
                resolved = candidate.resolve()
                sources.append(resolved)
                seen.add(str(resolved).lower())
    return sorted(sources, key=lambda path: (path.parent.name.lower(), path.name.lower()))


# --- Per-parent-task vocabulary + progress ---------------------------------


def _empty_tasks_doc() -> dict:
    return {"version": 1, "tasks": {}, "updated_at": _now_iso()}


def load_tasks_doc(root: Path) -> dict:
    path = tasks_path(root)
    with _lock:
        if not path.is_file():
            doc = _empty_tasks_doc()
            _atomic_write(path, doc)
            return doc
        try:
            raw = _read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return _empty_tasks_doc()
    tasks = raw.get("tasks") if isinstance(raw, dict) else {}
    if not isinstance(tasks, dict):
        tasks = {}
    normalized: dict[str, dict] = {}
    for name, cfg in tasks.items():
        key = str(name).strip()
        if not key:
            continue
        if isinstance(cfg, dict):
            labels = [
                str(label).strip()
                for label in (cfg.get("labels") or cfg.get("subtasks") or [])
                if str(label).strip()
            ]
            target = cfg.get("target_hours")
            try:
                target_hours = float(target) if target is not None else None
            except (TypeError, ValueError):
                target_hours = None
        elif isinstance(cfg, list):
            labels = [str(label).strip() for label in cfg if str(label).strip()]
            target_hours = None
        else:
            continue
        # de-dupe case-insensitively, keep first spelling
        seen: set[str] = set()
        unique: list[str] = []
        for label in labels:
            low = label.lower()
            if low in seen:
                continue
            seen.add(low)
            unique.append(label)
        normalized[key] = {"labels": unique, "target_hours": target_hours}
    return {"version": 1, "tasks": normalized, "updated_at": raw.get("updated_at") or _now_iso()}


def save_tasks_doc(root: Path, doc: dict) -> dict:
    payload = {
        "version": 1,
        "tasks": doc.get("tasks") or {},
        "updated_at": _now_iso(),
    }
    with _lock:
        _atomic_write(tasks_path(root), payload)
    return payload


def _task_dir_for_name(root: Path, parent_task: str) -> Path | None:
    root = Path(root).expanduser().resolve()
    task = parent_task.strip().lower()
    for segment_path in root.rglob(SEGMENT_FILE):
        try:
            raw = _read_json(segment_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if str(raw.get("main_task") or raw.get("parent_task") or "").strip().lower() == task:
            return segment_path.parent.resolve()
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_dir() and path.name.strip().lower() == task
    ]
    candidates.sort(
        key=lambda path: (
            not any(child.suffix.lower() in {".mp4", ".mov"} for child in path.iterdir()),
            -len(path.parts),
        )
    )
    return candidates[0].resolve() if candidates else None


def _empty_manifest() -> dict:
    return {"version": VERSION, "subtasks": [], "updated_at": _now_iso()}


def subtask_folder_name(name: str, subtask_id: str) -> str:
    return f"{safe_label_name(name)}-{str(subtask_id).zfill(3)}"


def _normalize_manifest(raw: dict | None) -> dict:
    subtasks: list[dict] = []
    seen_names: set[str] = set()
    used_ids: set[str] = set()
    for index, item in enumerate((raw or {}).get("subtasks") or [], start=1):
        if isinstance(item, str):
            item = {"name": item}
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("label") or "").strip()
        if not name or name.lower() in seen_names:
            continue
        raw_id = str(item.get("id") or "").strip()
        subtask_id = raw_id.zfill(3) if raw_id.isdigit() else f"{index:03d}"
        while subtask_id in used_ids:
            subtask_id = f"{int(subtask_id) + 1:03d}"
        clips = [
            dict(clip)
            for clip in item.get("clips") or []
            if isinstance(clip, dict) and str(clip.get("filename") or "").strip()
        ]
        clips.sort(key=lambda clip: int(clip.get("video_serial") or 0))
        subtasks.append(
            {
                "id": subtask_id,
                "name": name,
                "folder": str(
                    item.get("folder") or subtask_folder_name(name, subtask_id)
                ),
                "total_clips": len(clips),
                "clips": clips,
            }
        )
        seen_names.add(name.lower())
        used_ids.add(subtask_id)
    return {
        "version": VERSION,
        "subtasks": subtasks,
        "updated_at": str((raw or {}).get("updated_at") or _now_iso()),
    }


def _migrate_clip_layout(task_dir: Path, manifest: dict) -> bool:
    """Move clips into stable subtask folders using CAMERA-ID-SERIAL names."""
    segment_path = task_dir / SEGMENT_FILE
    try:
        segment_doc = _read_json(segment_path) if segment_path.is_file() else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        segment_doc = None
    camera_by_source = {}
    if isinstance(segment_doc, dict):
        camera_by_source = {
            str(video.get("source_video") or "").lower(): str(
                video.get("camera_serial") or "UNKNOWN"
            )
            for video in segment_doc.get("videos") or []
        }

    renamed: dict[str, tuple[str, str]] = {}
    changed = False
    for subtask in manifest["subtasks"]:
        folder = subtask_folder_name(subtask["name"], subtask["id"])
        if subtask.get("folder") != folder:
            subtask["folder"] = folder
            changed = True
        target_dir = task_dir / folder
        target_dir.mkdir(parents=True, exist_ok=True)
        for clip in subtask.get("clips") or []:
            old_name = str(clip.get("filename") or "").strip()
            camera = re.sub(
                r"[^A-Za-z0-9]+",
                "",
                str(
                    clip.get("camera_serial")
                    or camera_by_source.get(
                        str(clip.get("source_video") or "").lower(), "UNKNOWN"
                    )
                ),
            ).upper() or "UNKNOWN"
            serial = int(clip.get("video_serial") or 0)
            if serial <= 0:
                continue
            new_name = f"{camera}-{subtask['id']}-{serial:03d}.mp4"
            new_path = target_dir / new_name
            candidates = [task_dir / old_name, target_dir / old_name]
            old_path = next((path for path in candidates if path.is_file()), None)
            if old_path is not None and old_path != new_path and not new_path.exists():
                old_path.replace(new_path)
            if old_name != new_name:
                clip["filename"] = new_name
                renamed[old_name] = (folder, new_name)
                changed = True
            if clip.get("camera_serial") != camera:
                clip["camera_serial"] = camera
                changed = True

        stitched_name = f"{folder}-stitched.mp4"
        stitched_target = target_dir / stitched_name
        for old_stitched in (
            task_dir / f"{safe_label_name(subtask['name'])}-stitched.mp4",
            task_dir / f"{safe_label_name(subtask['name'])}__stitched.MP4",
        ):
            if old_stitched.is_file() and not stitched_target.exists():
                old_stitched.replace(stitched_target)
                changed = True
                break

    if renamed and isinstance(segment_doc, dict):
        for video in segment_doc.get("videos") or []:
            for segment in video.get("segments") or []:
                old_name = str(segment.get("clip_filename") or "")
                if old_name in renamed:
                    folder, new_name = renamed[old_name]
                    segment["clip_filename"] = new_name
                    segment["clip_path"] = f"{folder}/{new_name}"
        segment_doc["updated_at"] = _now_iso()
        _atomic_write(segment_path, segment_doc)
    return changed


def load_manifest(path: Path) -> dict:
    manifest_path = Path(path)
    if manifest_path.is_dir():
        manifest_path = manifest_path / MANIFEST_FILE
    if not manifest_path.is_file():
        return _empty_manifest()
    try:
        raw = _read_json(manifest_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _empty_manifest()
    manifest = _normalize_manifest(raw if isinstance(raw, dict) else None)
    if _migrate_clip_layout(manifest_path.parent, manifest):
        manifest["updated_at"] = _now_iso()
        _atomic_write(manifest_path, manifest)
    return manifest


def _save_manifest(task_dir: Path, manifest: dict) -> dict:
    normalized = _normalize_manifest(manifest)
    normalized["updated_at"] = _now_iso()
    for subtask in normalized["subtasks"]:
        (Path(task_dir) / subtask["folder"]).mkdir(parents=True, exist_ok=True)
    _atomic_write(Path(task_dir) / MANIFEST_FILE, normalized)
    return normalized


def _ensure_manifest_from_annotation(source: Path, annotation: dict) -> dict:
    """Create stable subtask IDs when opening an old per-video annotation."""
    labels: list[str] = []
    seen: set[str] = set()
    for segment in annotation.get("segments") or []:
        if str(segment.get("type") or "").lower() != "subtask":
            continue
        label = str(segment.get("label") or "").strip()
        if label and label.lower() not in seen:
            labels.append(label)
            seen.add(label.lower())
    task_dir = task_directory(source)
    manifest = load_manifest(task_dir)
    existing = {row["name"].lower() for row in manifest["subtasks"]}
    changed = False
    next_id = max(
        (int(row["id"]) for row in manifest["subtasks"] if str(row["id"]).isdigit()),
        default=0,
    ) + 1
    for label in labels:
        if label.lower() in existing:
            continue
        manifest["subtasks"].append(
            {
                "id": f"{next_id:03d}",
                "name": label,
                "folder": subtask_folder_name(label, f"{next_id:03d}"),
                "total_clips": 0,
                "clips": [],
            }
        )
        existing.add(label.lower())
        next_id += 1
        changed = True
    return _save_manifest(task_dir, manifest) if changed else manifest


def labels_for_task(root: Path, parent_task: str) -> list[str]:
    task_dir = _task_dir_for_name(root, parent_task)
    if task_dir is None:
        return []
    return [row["name"] for row in load_manifest(task_dir)["subtasks"]]


def add_label(root: Path, parent_task: str, label: str) -> list[str]:
    task = parent_task.strip()
    clean = label.strip()
    if not task or not clean:
        return labels_for_task(root, task)
    with _lock:
        task_dir = _task_dir_for_name(root, task)
        if task_dir is None:
            raise ValueError(f"Could not find folder for main task: {task}")
        manifest = load_manifest(task_dir)
        if not any(row["name"].lower() == clean.lower() for row in manifest["subtasks"]):
            next_id = max(
                (int(row["id"]) for row in manifest["subtasks"] if str(row["id"]).isdigit()),
                default=0,
            ) + 1
            manifest["subtasks"].append(
                {"id": f"{next_id:03d}", "name": clean, "total_clips": 0, "clips": []}
            )
            manifest = _save_manifest(task_dir, manifest)
    return [row["name"] for row in manifest["subtasks"]]


def set_task_target(root: Path, parent_task: str, target_hours: float | None) -> dict:
    task = parent_task.strip()
    with _lock:
        doc = load_tasks_doc(root)
        tasks = dict(doc.get("tasks") or {})
        cfg = dict(tasks.get(task) or {"labels": [], "target_hours": None})
        cfg["target_hours"] = None if target_hours is None else float(target_hours)
        tasks[task] = cfg
        doc["tasks"] = tasks
        save_tasks_doc(root, doc)
    return refresh_progress(root)


def usable_seconds(annotation: dict) -> float:
    total = 0.0
    for segment in annotation.get("segments") or []:
        if str(segment.get("type") or "").lower() != "subtask":
            continue
        try:
            total += float(segment.get("duration") or (float(segment["end"]) - float(segment["start"])))
        except (TypeError, ValueError, KeyError):
            continue
    return total


def refresh_progress(root: Path) -> dict:
    """Recompute labeled usable hours per parent task under the dataset root."""
    root = Path(root).expanduser().resolve()
    tasks_doc = load_tasks_doc(root)
    by_task: dict[str, dict] = {}

    for sidecar in iter_sidecars(root):
        try:
            doc = _read_json(sidecar)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        parent = str(doc.get("main_task") or doc.get("parent_task") or sidecar.parent.name).strip()
        for raw_annotation in doc.get("videos") or []:
            source = Path(str(raw_annotation.get("source_path") or ""))
            if not source.is_file():
                source = sidecar.parent / str(raw_annotation.get("source_video") or "")
            if not source.is_file():
                continue
            annotation = normalize_annotation(raw_annotation, source, root=root)
            entry = by_task.setdefault(
                parent,
                {
                    "task": parent,
                    "labeled_seconds": 0.0,
                    "video_count": 0,
                    "labeled_video_count": 0,
                },
            )
            entry["video_count"] += 1
            seconds = usable_seconds(annotation)
            entry["labeled_seconds"] += seconds
            if seconds > 0:
                entry["labeled_video_count"] += 1

    # Ensure configured tasks appear even with zero videos.
    for name, cfg in (tasks_doc.get("tasks") or {}).items():
        by_task.setdefault(
            name,
            {
                "task": name,
                "labeled_seconds": 0.0,
                "video_count": 0,
                "labeled_video_count": 0,
            },
        )
        by_task[name]["target_hours"] = cfg.get("target_hours")
        by_task[name]["labels"] = labels_for_task(root, name)

    rows = []
    for name, entry in sorted(by_task.items(), key=lambda item: item[0].lower()):
        cfg = (tasks_doc.get("tasks") or {}).get(name) or {}
        labeled_hours = float(entry["labeled_seconds"]) / 3600.0
        target = cfg.get("target_hours")
        try:
            target_hours = float(target) if target is not None else None
        except (TypeError, ValueError):
            target_hours = None
        remaining = None if target_hours is None else max(0.0, target_hours - labeled_hours)
        complete = bool(target_hours is not None and labeled_hours + 1e-9 >= target_hours)
        rows.append(
            {
                "task": name,
                "target_hours": target_hours,
                "labeled_hours": round(labeled_hours, 4),
                "remaining_hours": None if remaining is None else round(remaining, 4),
                "percent_complete": (
                    None
                    if target_hours is None or target_hours <= 0
                    else round(min(100.0, (labeled_hours / target_hours) * 100.0), 1)
                ),
                "complete": complete,
                "video_count": int(entry["video_count"]),
                "labeled_video_count": int(entry["labeled_video_count"]),
                "labels": labels_for_task(root, name),
            }
        )

    payload = {
        "version": 1,
        "root": str(root),
        "updated_at": _now_iso(),
        "tasks": rows,
    }
    with _lock:
        _atomic_write(progress_path(root), payload)
    return payload


def load_progress(root: Path, *, refresh: bool = False) -> dict:
    root = Path(root).expanduser().resolve()
    path = progress_path(root)
    if refresh or not path.is_file():
        return refresh_progress(root)
    try:
        return _read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return refresh_progress(root)


def export_directory(source: Path) -> Path:
    """Main-task folder containing sources, clips, JSON, and stitched outputs."""
    return task_directory(source)


def subtask_export_directory(source: Path, label: str) -> Path:
    manifest = load_manifest(manifest_path_for(source))
    subtask = next(
        (row for row in manifest["subtasks"] if row["name"].lower() == label.strip().lower()),
        None,
    )
    if subtask is None:
        raise ValueError(f"Subtask is not defined in manifest.json: {label}")
    return export_directory(source) / subtask["folder"]


def next_clip_filename(
    source: Path,
    label: str,
    output_dir: Path,
    *,
    reserved: set[str] | None = None,
) -> str:
    """``CAMERASERIAL-SUBTASKID-CLIPSERIAL.mp4`` in the subtask folder."""
    annotation = load_annotation(source)
    camera = re.sub(
        r"[^A-Za-z0-9]+",
        "",
        str(annotation.get("camera_serial") or annotation.get("cl_number") or "UNKNOWN"),
    ).upper() or "UNKNOWN"
    manifest = load_manifest(manifest_path_for(source))
    subtask = next(
        (row for row in manifest["subtasks"] if row["name"].lower() == label.strip().lower()),
        None,
    )
    if subtask is None:
        raise ValueError(f"Subtask is not defined in manifest.json: {label}")
    prefix = f"{camera}-{subtask['id']}-"
    highest = 0
    claimed = {name.lower() for name in (reserved or set())}
    if output_dir.is_dir():
        for path in output_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".mp4", ".mov"}:
                continue
            claimed.add(path.name.lower())
            match = CLIP_NAME_RE.match(path.name)
            if match and match.group("subtask") == subtask["id"]:
                highest = max(highest, int(match.group("clip")))
    for clip in subtask.get("clips") or []:
        try:
            highest = max(highest, int(clip.get("video_serial") or 0))
        except (TypeError, ValueError):
            pass
    for name in list(claimed):
        match = CLIP_NAME_RE.match(name)
        if match and match.group("subtask") == subtask["id"]:
            highest = max(highest, int(match.group("clip")))
    candidate = highest + 1
    while True:
        filename = f"{prefix}{candidate:03d}.mp4"
        if filename.lower() not in claimed:
            return filename
        candidate += 1


def list_subtask_export_dirs(source: Path) -> list[Path]:
    return [clips[0].parent for _subtask, clips in clips_by_subtask(source)]


def clips_by_subtask(source: Path) -> list[tuple[dict, list[Path]]]:
    task_dir = export_directory(source)
    manifest = load_manifest(task_dir)
    result: list[tuple[dict, list[Path]]] = []
    for subtask in manifest["subtasks"]:
        subtask_dir = task_dir / subtask["folder"]
        clips = [
            subtask_dir / str(row["filename"])
            for row in subtask.get("clips") or []
            if (subtask_dir / str(row["filename"])).is_file()
        ]
        if not clips and subtask_dir.is_dir():
            clips = sorted(
                (
                    path
                    for path in subtask_dir.iterdir()
                    if path.is_file()
                    and (match := CLIP_NAME_RE.match(path.name))
                    and match.group("subtask") == subtask["id"]
                ),
                key=lambda path: int(CLIP_NAME_RE.match(path.name).group("clip")),
            )
        if clips:
            result.append((subtask, clips))
    return result


def write_export_manifest(source: Path, rows: list[dict]) -> Path:
    task_dir = export_directory(source)
    manifest = load_manifest(task_dir)
    for row in rows:
        label = str(row.get("subtask") or "").strip()
        subtask = next(
            (item for item in manifest["subtasks"] if item["name"].lower() == label.lower()),
            None,
        )
        if subtask is None:
            continue
        filename = str(row.get("clip_filename") or "").strip()
        match = CLIP_NAME_RE.match(filename)
        clip = {
            "camera_serial": str(row.get("camera_serial") or "UNKNOWN"),
            "video_serial": int(match.group("clip")) if match else len(subtask["clips"]) + 1,
            "filename": filename,
            "source_video": str(row.get("source_video") or ""),
        }
        by_name = {
            str(existing.get("filename") or ""): existing
            for existing in subtask.get("clips") or []
        }
        by_name[filename] = clip
        subtask["clips"] = sorted(
            by_name.values(), key=lambda item: int(item.get("video_serial") or 0)
        )
        subtask["total_clips"] = len(subtask["clips"])
    _save_manifest(task_dir, manifest)
    return task_dir / MANIFEST_FILE


def is_export_path(path: Path, root: Path | None = None) -> bool:
    """True for generated short clips and per-subtask stitched outputs."""
    path = Path(path).expanduser().resolve()
    del root
    if path.name.lower().endswith("-stitched.mp4"):
        return True
    if CLIP_NAME_RE.match(path.name) or SOURCE_CLIP_NAME_RE.match(path.name):
        return True
    manifest = load_manifest(path.parent)
    return any(
        str(clip.get("filename") or "").lower() == path.name.lower()
        for subtask in manifest["subtasks"]
        for clip in subtask.get("clips") or []
    )

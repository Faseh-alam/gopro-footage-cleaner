"""Adjacent ``VIDEO.json`` annotations for the Scale AI 50-hour workflow.

Free-form subtask / garbage segments (gaps allowed). Parent task is the first
folder under the opened 50-hour root. Labels and progress live under
``<root>/_labeling/``.
"""

from __future__ import annotations

import csv
import json
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .annotation_store import MIN_SEGMENT, normalize_boundary, resolve_media_duration

VERSION = 1
SIDECAR_SUFFIX = ".json"
LABELING_DIR = "_labeling"
TASKS_FILE = "tasks.json"
PROGRESS_FILE = "progress.json"
EPSILON = 0.001
CL_RE = re.compile(r"(?i)\b(C\d{3,6}|CL[-_]?\w+)\b")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

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


def safe_label_name(label: str) -> str:
    cleaned = SAFE_NAME_RE.sub("-", (label or "").strip()).strip("-._")
    return cleaned or "untitled"


def sidecar_path_for(video: Path) -> Path:
    path = Path(video)
    return path.with_name(f"{path.stem}{SIDECAR_SUFFIX}")


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
        segments.append(
            {
                "id": item.get("id") or index,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "type": seg_type,
                "label": label,
            }
        )
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
        "source_video": str(raw.get("source_video") or source.name),
        "source_path": str(raw.get("source_path") or source),
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
            # Do not create the JSON until the first label is saved.
            return empty_annotation(source, root=root)
        raw = json.loads(sidecar.read_text(encoding="utf-8"))
        annotation = normalize_annotation(raw, source, root=root)
        # Heal metadata / duration without rewriting unless the file already exists
        # and content meaningfully changed.
        if annotation != raw:
            annotation["updated_at"] = _now_iso()
            _atomic_write(sidecar, annotation)
        return annotation


def save_annotation(video: Path, annotation: dict, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    normalized = normalize_annotation(annotation, source, root=root)
    normalized["updated_at"] = _now_iso()
    with _lock:
        _atomic_write(sidecar_path_for(source), normalized)
    if root is not None:
        refresh_progress(Path(root))
    return normalized


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
        _atomic_write(sidecar_path_for(source), annotation)
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
        _atomic_write(sidecar_path_for(source), annotation)
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
        _atomic_write(sidecar_path_for(source), annotation)
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
        _atomic_write(sidecar_path_for(source), annotation)
    if root is not None:
        refresh_progress(Path(root))
    return annotation


def iter_sidecars(root: Path):
    root = Path(root).expanduser().resolve()
    for path in root.rglob(f"*{SIDECAR_SUFFIX}"):
        if not path.is_file():
            continue
        if any(part.lower() == LABELING_DIR for part in path.parts):
            continue
        # Only adjacent-to-mp4 annotations: stem.MP4 next to stem.json
        if not any(
            (path.parent / f"{path.stem}{ext}").is_file()
            for ext in (".MP4", ".mp4", ".MOV", ".mov")
        ):
            continue
        yield path


def source_for_sidecar(sidecar: Path) -> Path | None:
    sidecar = Path(sidecar)
    for ext in (".MP4", ".mp4", ".MOV", ".mov"):
        candidate = sidecar.with_name(f"{sidecar.stem}{ext}")
        if candidate.is_file():
            return candidate.resolve()
    return None


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
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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


def labels_for_task(root: Path, parent_task: str) -> list[str]:
    doc = load_tasks_doc(root)
    cfg = (doc.get("tasks") or {}).get(parent_task.strip()) or {}
    return list(cfg.get("labels") or [])


def add_label(root: Path, parent_task: str, label: str) -> list[str]:
    task = parent_task.strip()
    clean = label.strip()
    if not task or not clean:
        return labels_for_task(root, task)
    with _lock:
        doc = load_tasks_doc(root)
        tasks = dict(doc.get("tasks") or {})
        cfg = dict(tasks.get(task) or {"labels": [], "target_hours": None})
        labels = list(cfg.get("labels") or [])
        if not any(existing.lower() == clean.lower() for existing in labels):
            labels.insert(0, clean)
        cfg["labels"] = labels
        tasks[task] = cfg
        doc["tasks"] = tasks
        save_tasks_doc(root, doc)
    return labels


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
        source = source_for_sidecar(sidecar)
        if source is None:
            continue
        try:
            annotation = normalize_annotation(
                json.loads(sidecar.read_text(encoding="utf-8")),
                source,
                root=root,
            )
        except (OSError, json.JSONDecodeError):
            continue
        parent = str(annotation.get("parent_task") or infer_parent_task(source, root)).strip()
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
        by_task[name]["labels"] = list(cfg.get("labels") or [])

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
                "labels": list(cfg.get("labels") or entry.get("labels") or []),
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
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return refresh_progress(root)


def export_directory(source: Path) -> Path:
    """``<parent>/<VIDEO_STEM>/`` next to the source MP4."""
    source = Path(source).expanduser().resolve()
    return source.parent / source.stem


def subtask_export_directory(source: Path, label: str) -> Path:
    return export_directory(source) / safe_label_name(label)


def next_clip_filename(
    source: Path,
    label: str,
    output_dir: Path,
    *,
    reserved: set[str] | None = None,
) -> str:
    """Simple per-subtask sequence: ``0001.MP4``, ``0002.MP4``, …"""
    del label  # folder already encodes the subtask label
    suffix = Path(source).suffix or ".MP4"
    highest = 0
    claimed = {name.lower() for name in (reserved or set())}
    if output_dir.is_dir():
        for path in output_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".mp4", ".mov"}:
                continue
            claimed.add(path.name.lower())
            name = path.stem
            # Prefer plain numeric names; also accept legacy STEM_label_0001.
            if name.isdigit():
                highest = max(highest, int(name))
                continue
            tail = name.rsplit("_", 1)[-1]
            if tail.isdigit():
                highest = max(highest, int(tail))
    for name in list(claimed):
        stem = Path(name).stem
        if stem.isdigit():
            highest = max(highest, int(stem))
    candidate = highest + 1
    while True:
        filename = f"{candidate:04d}{suffix}"
        if filename.lower() not in claimed:
            return filename
        candidate += 1


def list_subtask_export_dirs(source: Path) -> list[Path]:
    """Subtask folders under ``<VIDEO_STEM>/`` that contain trim clips."""
    export_root = export_directory(source)
    if not export_root.is_dir():
        return []
    dirs: list[Path] = []
    for path in sorted(export_root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        if path.name.lower() in {"_labeling"}:
            continue
        has_clip = any(
            child.is_file()
            and child.suffix.lower() in {".mp4", ".mov"}
            and "__stitched" not in child.name.lower()
            and not child.name.lower().startswith("stitched")
            for child in path.iterdir()
        )
        if has_clip:
            dirs.append(path)
    return dirs


def write_export_manifest(source: Path, rows: list[dict]) -> Path:
    export_dir = export_directory(source)
    export_dir.mkdir(parents=True, exist_ok=True)
    manifest = export_dir / "export_manifest.csv"
    fieldnames = [
        "clip_filename",
        "source_video",
        "parent_task",
        "subtask",
        "source_start",
        "source_end",
        "duration",
        "camera_serial",
        "cl_number",
    ]
    existing: list[dict] = []
    if manifest.is_file():
        with manifest.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                existing.append(dict(row))
    # Replace rows for overlapping clip filenames; append new ones.
    by_name = {str(row.get("clip_filename") or ""): row for row in existing}
    for row in rows:
        by_name[str(row.get("clip_filename") or "")] = row
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for name in sorted(by_name):
            if not name:
                continue
            writer.writerow({key: by_name[name].get(key, "") for key in fieldnames})
    return manifest


def is_export_path(path: Path, root: Path | None = None) -> bool:
    """True when ``path`` sits inside a per-video export folder."""
    path = Path(path).expanduser().resolve()
    resolved_root = Path(root).expanduser().resolve() if root is not None else None
    for ancestor in path.parents:
        if resolved_root is not None:
            try:
                ancestor.relative_to(resolved_root)
            except ValueError:
                break
        if (ancestor / "export_manifest.csv").is_file():
            return True
        # Export root is ``<stem>/`` sitting next to ``<stem>.MP4``.
        sibling = ancestor.parent / f"{ancestor.name}.MP4"
        sibling_l = ancestor.parent / f"{ancestor.name}.mp4"
        if sibling.is_file() or sibling_l.is_file():
            return True
    return False

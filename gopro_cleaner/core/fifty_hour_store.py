"""Task-level JSON storage for the Scale AI 50-hour workflow.

Each source video owns ``{stem}.json`` (timestamps and camera metadata). Each
main-task folder also owns one ``manifest.json`` with stable subtask IDs and
generated short clips. Free-form subtask / garbage gaps are allowed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .annotation_store import MIN_SEGMENT, normalize_boundary, resolve_media_duration

VERSION = 2
LEGACY_SEGMENT_FILE = "segment.json"
SEGMENT_FILE = LEGACY_SEGMENT_FILE  # leftover combined task document
MANIFEST_FILE = "manifest.json"
SIDECAR_SUFFIX = ".json"
LABELING_DIR = "_labeling"
TASKS_FILE = "tasks.json"
PROGRESS_FILE = "progress.json"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".ts"}
UNLABELED_TASK_LABEL = "Unlabeled task"
JUNK_JSON_SUFFIXES = (
    ".segments.json",
    ".scaleai.json",
    ".scaleai-source.json",
)
JUNK_TXT_SUFFIXES = (".segments.txt",)
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
    """Replace ``path`` in place. Never leave a sibling temp JSON behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


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
    """Per-source annotation file: ``video2.mp4`` → ``video2.json``."""
    source = Path(video).expanduser().resolve()
    return source.with_name(f"{source.stem}{SIDECAR_SUFFIX}")


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


def _is_source_video_file(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    name = path.name
    if name.lower().endswith("-stitched.mp4"):
        return False
    if CLIP_NAME_RE.match(name) or SOURCE_CLIP_NAME_RE.match(name):
        return False
    return True


def source_videos_in_task(task_dir: Path) -> list[Path]:
    folder = Path(task_dir)
    if not folder.is_dir():
        return []
    return sorted(
        (path for path in folder.iterdir() if _is_source_video_file(path)),
        key=lambda path: path.name.lower(),
    )


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


def _sidecar_destination_for_row(task_dir: Path, row: dict) -> Path | None:
    source_path = Path(str(row.get("source_path") or "")).expanduser()
    source_name = str(row.get("source_video") or "").strip()
    if source_path.is_file():
        return sidecar_path_for(source_path)
    if source_name:
        return Path(task_dir) / f"{Path(source_name).stem}{SIDECAR_SUFFIX}"
    return None


def _write_video_json(dest: Path, raw: dict, source: Path, *, root: Path | None = None) -> None:
    try:
        payload = normalize_annotation(raw, source, root=root)
    except Exception:  # noqa: BLE001
        payload = raw if isinstance(raw, dict) else {}
    _atomic_write(dest, payload)


def _split_legacy_segment_json(task_dir: Path, *, root: Path | None = None) -> None:
    """Turn leftover ``segment.json`` into one ``{stem}.json`` per source video."""
    task_dir = Path(task_dir)
    legacy = task_dir / LEGACY_SEGMENT_FILE
    if not legacy.is_file():
        return
    doc = _read_segment_doc(legacy, task_dir.name)
    for row in doc["videos"]:
        dest = _sidecar_destination_for_row(task_dir, row)
        if dest is None or dest.is_file():
            continue
        source_path = Path(str(row.get("source_path") or "")).expanduser()
        if not source_path.is_file():
            name = str(row.get("source_video") or dest.stem).strip()
            source_path = task_dir / name
        if isinstance(row.get("segments"), list) and row.get("source_video"):
            payload = dict(row)
            if source_path.is_file():
                payload["source_path"] = str(source_path.resolve())
            _atomic_write(dest, payload)
        else:
            _write_video_json(dest, row, source_path, root=root)
    legacy.unlink(missing_ok=True)


def _promote_segments_json(task_dir: Path, *, root: Path | None = None) -> None:
    """``GX020399.segments.json`` → ``GX020399.json`` when the per-video file is missing."""
    for video in source_videos_in_task(task_dir):
        dest = sidecar_path_for(video)
        if dest.is_file():
            continue
        old = video.with_name(f"{video.stem}.segments.json")
        if not old.is_file():
            continue
        try:
            raw = _read_json(old)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        _write_video_json(dest, raw, video, root=root)


def _is_kept_task_json(path: Path, task_dir: Path) -> bool:
    if path.parent.resolve() != Path(task_dir).resolve():
        return False
    name = path.name.lower()
    if name == MANIFEST_FILE.lower():
        return True
    if name == LEGACY_SEGMENT_FILE.lower():
        return False
    return any(video.stem.lower() == path.stem.lower() for video in source_videos_in_task(task_dir))


def cleanup_task_folder_files(task_dir: Path, *, root: Path | None = None) -> None:
    """Drop leftover txt/json sidecars; keep per-video JSON and ``manifest.json``."""
    task_dir = Path(task_dir)
    if not task_dir.is_dir():
        return
    _split_legacy_segment_json(task_dir, root=root)
    _promote_segments_json(task_dir, root=root)
    for path in list(task_dir.rglob("*")):
        if not path.is_file():
            continue
        if LABELING_DIR in path.parts:
            continue
        if path.name.startswith("._"):
            if path.suffix.lower() in {".json", ".txt", ".tmp"}:
                path.unlink(missing_ok=True)
            continue
        lower = path.name.lower()
        if lower.endswith(".tmp"):
            path.unlink(missing_ok=True)
            continue
        if any(lower.endswith(suffix) for suffix in JUNK_TXT_SUFFIXES):
            path.unlink(missing_ok=True)
            continue
        if any(lower.endswith(suffix) for suffix in JUNK_JSON_SUFFIXES):
            path.unlink(missing_ok=True)
            continue
        if lower.endswith(".manifest.json") and lower != MANIFEST_FILE.lower():
            path.unlink(missing_ok=True)
            continue
        if path.suffix.lower() != ".json":
            continue
        if _is_kept_task_json(path, task_dir):
            continue
        path.unlink(missing_ok=True)
    leftover = task_dir / LEGACY_SEGMENT_FILE
    leftover.unlink(missing_ok=True)


def _raw_annotation_rows(sidecar: Path) -> list[dict]:
    try:
        raw = _read_json(sidecar)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    if sidecar.name.lower() == LEGACY_SEGMENT_FILE.lower() and isinstance(
        raw.get("videos"), list
    ):
        return [row for row in raw["videos"] if isinstance(row, dict)]
    return [raw]


def normalize_annotation(raw: dict, video: Path, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    base = empty_annotation(source, root=root)
    if not isinstance(raw, dict):
        return base

    claimed = raw.get("duration_seconds", raw.get("duration"))
    try:
        claimed_f = float(claimed) if claimed is not None else None
        if claimed_f is not None and claimed_f <= 0:
            claimed_f = None
    except (TypeError, ValueError):
        claimed_f = None
    # Skip ffprobe on every T/Enter when JSON already has a duration.
    duration = (
        claimed_f
        if claimed_f is not None
        else resolve_media_duration(source, claimed)
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
        raw_id = item.get("id")
        if raw_id is None or raw_id == "":
            seg_id: int | str = index
        else:
            key = _segment_id_key(raw_id)
            seg_id = int(key) if key.isdigit() else raw_id
        segment = {
            "id": seg_id,
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


def load_annotation(
    video: Path, *, root: Path | None = None, repair: bool = True
) -> dict:
    source = Path(video).expanduser().resolve()
    sidecar = sidecar_path_for(source)
    with _lock:
        if repair:
            cleanup_task_folder_files(task_directory(source), root=root)
        if not sidecar.is_file():
            return empty_annotation(source, root=root)
        try:
            raw = _read_json(sidecar)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raw = {}
        annotation = normalize_annotation(
            raw if isinstance(raw, dict) else {}, source, root=root
        )
        _ensure_manifest_from_annotation(source, annotation, repair=repair)
        # Manifest loading may migrate clip files into subtask folders and update
        # clip references in this video's JSON. Return that migrated annotation.
        if sidecar.is_file():
            try:
                refreshed = _read_json(sidecar)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                refreshed = raw
            annotation = normalize_annotation(
                refreshed if isinstance(refreshed, dict) else {}, source, root=root
            )
        if repair and _sync_segment_clips(source, annotation):
            annotation["updated_at"] = _now_iso()
            _atomic_write(sidecar, annotation)
        return annotation


def save_annotation(
    video: Path,
    annotation: dict,
    *,
    root: Path | None = None,
    cleanup: bool = True,
) -> dict:
    source = Path(video).expanduser().resolve()
    normalized = normalize_annotation(annotation, source, root=root)
    normalized["updated_at"] = _now_iso()
    with _lock:
        if cleanup:
            cleanup_task_folder_files(task_directory(source), root=root)
        _atomic_write(sidecar_path_for(source), normalized)
    if root is not None:
        refresh_progress(Path(root))
    return normalized


def remove_video_annotation(video: Path) -> None:
    """Delete this source video's JSON. Shared ``manifest.json`` is left in place."""
    source = Path(video).expanduser().resolve()
    path = sidecar_path_for(source)
    task_dir = task_directory(source)
    with _lock:
        path.unlink(missing_ok=True)
        legacy = task_dir / LEGACY_SEGMENT_FILE
        if legacy.is_file():
            doc = _read_segment_doc(legacy, source.parent.name)
            source_key = str(source).lower()
            doc["videos"] = [
                row
                for row in doc["videos"]
                if str(row.get("source_path") or "").lower() != source_key
                and str(row.get("source_video") or "").lower() != source.name.lower()
            ]
            doc["updated_at"] = _now_iso()
            if doc["videos"]:
                _atomic_write(legacy, doc)
            else:
                legacy.unlink(missing_ok=True)


def _segment_id_key(value) -> str:
    raw = str(value).strip() if value is not None else ""
    if not raw:
        return ""
    try:
        num = float(raw)
        if num.is_integer():
            return str(int(num))
    except (TypeError, ValueError):
        pass
    return raw


def _find_segment(annotation: dict, segment_id) -> dict | None:
    target = _segment_id_key(segment_id)
    if not target:
        return None
    for segment in annotation.get("segments") or []:
        if _segment_id_key(segment.get("id")) == target:
            return segment
    return None


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
    duration: float | None = None,
) -> tuple[float, float]:
    """Bump ``start`` by 0.01s when it overlaps or shares an end boundary.

    Only previous-segment end collisions are adjusted — never bump because our
    end touches a later segment's start. When the leftover span is shorter
    than ``MIN_SEGMENT``, extend ``end`` if the video still has room.
    """
    start_n = float(start)
    end_n = float(end)
    ignore_key = _segment_id_key(ignore_id) if ignore_id is not None else ""
    for _ in range(max(8, len(existing_segments) + 2)):
        conflict_end: float | None = None
        for existing in existing_segments:
            if ignore_key and _segment_id_key(existing.get("id")) == ignore_key:
                continue
            es = float(existing["start"])
            ee = float(existing["end"])
            overlaps = max(start_n, es) < min(end_n, ee) - EPSILON
            shares_end = abs(start_n - ee) <= EPSILON
            if overlaps or shares_end:
                conflict_end = ee if conflict_end is None else max(conflict_end, ee)
        if conflict_end is None:
            return round(start_n, 3), round(end_n, 3)
        start_n = round(conflict_end + BOUNDARY_GAP, 3)
        if end_n - start_n < MIN_SEGMENT - EPSILON:
            needed_end = round(start_n + MIN_SEGMENT, 3)
            if duration is not None:
                try:
                    needed_end = min(needed_end, float(duration))
                except (TypeError, ValueError):
                    pass
            if needed_end - start_n >= MIN_SEGMENT - EPSILON:
                end_n = max(end_n, needed_end)
                continue
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
        annotation = load_annotation(source, root=root, repair=False)
        duration = annotation.get("duration_seconds")
        start_n = normalize_boundary(float(start), duration)
        end_n = normalize_boundary(float(end), duration)
        seg_type = str(segment_type or "subtask").strip().lower()
        if seg_type in {"work", "task"}:
            seg_type = "subtask"
        if seg_type not in {"subtask", "garbage"}:
            raise ValueError("type must be subtask or garbage")
        clean_label = "garbage" if seg_type == "garbage" else str(label or "").strip()
        if seg_type == "subtask" and not clean_label:
            raise ValueError("label is required for subtask segments")
        start_n, end_n = _resolve_non_overlapping_start(
            start_n,
            end_n,
            list(annotation.get("segments") or []),
            duration=duration,
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
        save_annotation(source, annotation, root=None, cleanup=False)
    if root is not None:
        if seg_type == "subtask":
            add_label(root, annotation["parent_task"], clean_label)
        refresh_progress(Path(root))
    return annotation


def delete_segment(video: Path, segment_id: str | int, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    target = _segment_id_key(segment_id)
    with _lock:
        annotation = load_annotation(source, root=root)
        before = len(annotation.get("segments") or [])
        annotation["segments"] = [
            segment
            for segment in annotation.get("segments") or []
            if _segment_id_key(segment.get("id")) != target
        ]
        if len(annotation["segments"]) == before:
            raise ValueError(f"Segment not found: {segment_id}")
        annotation["updated_at"] = _now_iso()
        save_annotation(source, annotation, root=root)
    if root is not None:
        refresh_progress(Path(root))
    refresh_manifest_durations(task_directory(source))
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
    target = _segment_id_key(segment_id)
    with _lock:
        annotation = load_annotation(source, root=root)
        duration = annotation.get("duration_seconds")
        found = _find_segment(annotation, segment_id)
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
        old_label = str(found.get("label") or "").strip()
        times_changed = start is not None or end is not None
        if times_changed:
            new_start, new_end = _resolve_non_overlapping_start(
                new_start,
                new_end,
                list(annotation.get("segments") or []),
                ignore_id=target,
                duration=duration,
            )
            if new_end - new_start < MIN_SEGMENT - EPSILON:
                raise ValueError(f"Segment too short (min {MIN_SEGMENT}s)")
        found["start"] = round(new_start, 3)
        found["end"] = round(new_end, 3)
        found["duration"] = round(new_end - new_start, 3)
        found["type"] = new_type
        found["label"] = new_label
        if new_type == "subtask" and old_label.lower() != new_label.lower():
            # Create the destination subtask before moving files so rehome
            # can resolve the new folder. RLock allows this nested call.
            if root is not None:
                add_label(root, annotation["parent_task"], new_label)
            _rehome_segment_clip(source, found, old_label, new_label, annotation)
            manifest = load_manifest(task_directory(source), repair=False)
            renamed = _compact_labeled_clip_serials(task_directory(source), manifest)
            _apply_renamed_clip_filenames(annotation, renamed, manifest)
        annotation["segments"].sort(key=lambda s: (float(s["start"]), float(s["end"])))
        annotation["updated_at"] = _now_iso()
        save_annotation(source, annotation, root=root)
    if root is not None:
        if new_type == "subtask":
            add_label(root, annotation["parent_task"], new_label)
        refresh_progress(Path(root))
    refresh_manifest_durations(task_directory(source))
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
    refresh_manifest_durations(task_directory(source))
    return annotation


def iter_sidecars(root: Path):
    root = Path(root).expanduser().resolve()
    seen: set[str] = set()
    for path in sorted(root.rglob("*.json")):
        if not path.is_file() or LABELING_DIR in path.parts:
            continue
        name = path.name.lower()
        if name.endswith(".tmp") or any(name.endswith(suffix) for suffix in JUNK_JSON_SUFFIXES):
            continue
        if name.endswith(".manifest.json") or name == MANIFEST_FILE.lower():
            continue
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        if name == LEGACY_SEGMENT_FILE.lower():
            seen.add(key)
            yield path
            continue
        if any(video.stem.lower() == path.stem.lower() for video in source_videos_in_task(path.parent)):
            seen.add(key)
            yield path


def source_for_sidecar(sidecar: Path) -> Path | None:
    sidecar = Path(sidecar).expanduser().resolve()
    for row in _raw_annotation_rows(sidecar):
        candidate = Path(str(row.get("source_path") or "")).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        name = str(row.get("source_video") or "").strip()
        if name:
            sibling = sidecar.parent / name
            if sibling.is_file():
                return sibling.resolve()
    for video in source_videos_in_task(sidecar.parent):
        if video.stem.lower() == sidecar.stem.lower():
            return video.resolve()
    return None


def annotated_sources(root: Path) -> list[Path]:
    root = Path(root).expanduser().resolve()
    pending = {sidecar.parent.resolve() for sidecar in iter_sidecars(root)}
    pending.update(
        path.parent.resolve()
        for path in root.rglob(LEGACY_SEGMENT_FILE)
        if LABELING_DIR not in path.parts
    )
    for task_dir in pending:
        cleanup_task_folder_files(task_dir, root=root)
    sources: list[Path] = []
    seen: set[str] = set()
    for sidecar in iter_sidecars(root):
        for row in _raw_annotation_rows(sidecar):
            candidate = Path(str(row.get("source_path") or "")).expanduser()
            if not candidate.is_file():
                name = str(row.get("source_video") or "").strip()
                candidate = sidecar.parent / name if name else Path()
            if not candidate.is_file():
                matched = source_for_sidecar(sidecar)
                candidate = matched if matched is not None else Path()
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
    for sidecar in iter_sidecars(root):
        try:
            raw = _read_json(sidecar)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        parent = str(
            raw.get("parent_task") or raw.get("main_task") or sidecar.parent.name
        ).strip()
        if parent.lower() == task:
            return sidecar.parent.resolve()
    candidates = [
        path
        for path in root.rglob("*")
        if path.is_dir() and path.name.strip().lower() == task
    ]
    candidates.sort(
        key=lambda path: (
            not any(child.suffix.lower() in VIDEO_EXTENSIONS for child in path.iterdir()),
            -len(path.parts),
        )
    )
    return candidates[0].resolve() if candidates else None


def _empty_manifest() -> dict:
    return {
        "version": VERSION,
        "subtasks": [],
        "total_duration_seconds": 0.0,
        "total_stitched_duration_seconds": 0.0,
        "updated_at": _now_iso(),
    }


def _as_seconds(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return round(number, 3)


def subtask_folder_name(name: str, subtask_id: str) -> str:
    return f"{safe_label_name(name)}-{str(subtask_id).zfill(3)}"


def _subtask_by_label(manifest: dict, label: str) -> dict | None:
    wanted = str(label or "").strip().lower()
    if not wanted:
        return None
    return next(
        (row for row in manifest.get("subtasks") or [] if row["name"].lower() == wanted),
        None,
    )


def _ensure_unlabeled_subtask(manifest: dict) -> dict:
    existing = _subtask_by_label(manifest, UNLABELED_TASK_LABEL)
    if existing is not None:
        return existing
    used = {str(row.get("id") or "") for row in manifest.get("subtasks") or []}
    next_id = 1
    while f"{next_id:03d}" in used:
        next_id += 1
    subtask_id = f"{next_id:03d}"
    row = {
        "id": subtask_id,
        "name": UNLABELED_TASK_LABEL,
        "folder": subtask_folder_name(UNLABELED_TASK_LABEL, subtask_id),
        "total_clips": 0,
        "clips": [],
    }
    manifest.setdefault("subtasks", []).append(row)
    return row


def _subtask_by_id(manifest: dict, subtask_id: str) -> dict | None:
    wanted = str(subtask_id or "").zfill(3)
    return next(
        (row for row in manifest.get("subtasks") or [] if str(row.get("id")) == wanted),
        None,
    )


def _camera_token(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "", str(value or "")).upper()


def _folder_subtask_id(folder_name: str) -> str | None:
    suffix = str(folder_name or "").rsplit("-", 1)
    if len(suffix) == 2 and suffix[1].isdigit() and len(suffix[1]) == 3:
        return suffix[1]
    return None


def retarget_clip_filename(
    filename: str,
    subtask_id: str,
    *,
    occupied: set[str] | None = None,
    reserved_serials: set[int] | None = None,
) -> str:
    """Keep camera + clip serial; swap only the middle subtask id.

    ``C346-001-053.mp4`` + subtask ``003`` → ``C346-003-053.mp4``.
    If this camera already has that filename, or another camera already
    uses that serial in the same folder, bump (053 → 054, …) so numbers
    stay unique in the folder.
    """
    match = CLIP_NAME_RE.match(str(filename or "").strip())
    if not match:
        return str(filename or "").strip()
    target_id = str(subtask_id).zfill(3)
    camera = match.group("camera")
    serial = int(match.group("clip"))
    claimed = {name.lower() for name in (occupied or set())}
    claimed.discard(str(filename).lower())
    used_serials = _serials_used_in_names(claimed)
    if reserved_serials:
        used_serials.update(reserved_serials)
    candidate = serial
    while True:
        name = f"{camera}-{target_id}-{candidate:03d}.mp4"
        if candidate not in used_serials and name.lower() not in claimed:
            return name
        candidate += 1


def _serials_used_in_names(names: set[str] | list[str]) -> set[int]:
    """Clip serials already used in a folder (any camera)."""
    used: set[int] = set()
    for name in names:
        parsed = CLIP_NAME_RE.match(str(name).strip())
        if parsed is not None:
            used.add(int(parsed.group("clip")))
    return used


def _cameras_holding_serials(names: set[str] | list[str]) -> dict[int, set[str]]:
    """Map clip serial → cameras that already use it (any subtask id)."""
    holders: dict[int, set[str]] = {}
    for name in names:
        parsed = CLIP_NAME_RE.match(str(name).strip())
        if parsed is None:
            continue
        holders.setdefault(int(parsed.group("clip")), set()).add(
            parsed.group("camera").upper()
        )
    return holders


def clip_serial_taken_by_other_camera(
    filename: str,
    occupied: set[str],
    *,
    reserved_serials: set[int] | None = None,
) -> bool:
    """True when another camera already uses this clip serial in the folder."""
    parsed = CLIP_NAME_RE.match(str(filename or "").strip())
    if parsed is None:
        return False
    serial = int(parsed.group("clip"))
    camera = parsed.group("camera").upper()
    holders = _cameras_holding_serials(occupied)
    others = set(holders.get(serial, set()))
    others.discard(camera)
    if others:
        return True
    if reserved_serials and serial in reserved_serials and camera not in holders.get(
        serial, set()
    ):
        return True
    return False


def _occupied_clip_names(folder: Path, extra: list[str] | None = None) -> set[str]:
    names = {str(name).lower() for name in (extra or []) if name}
    if folder.is_dir():
        for path in folder.iterdir():
            if path.is_file() and path.suffix.lower() in {".mp4", ".mov"}:
                names.add(path.name.lower())
    return names


def _clip_search_dirs(task_dir: Path, manifest: dict) -> list[Path]:
    dirs = [Path(task_dir)]
    for subtask in manifest.get("subtasks") or []:
        folder = str(subtask.get("folder") or "")
        if folder:
            dirs.append(Path(task_dir) / folder)
    return dirs


def _clip_identity_from_name(filename: object) -> tuple[str, int] | None:
    parsed = CLIP_NAME_RE.match(str(filename or "").strip())
    if parsed is None:
        return None
    return parsed.group("camera").upper(), int(parsed.group("clip"))


def _iter_identity_files(
    task_dir: Path,
    camera: str,
    serial: int,
    manifest: dict,
    *,
    old_id: str | None = None,
) -> list[Path]:
    """Copies of one clip: leftover old-id names, or files sitting in the wrong folder.

    Clip serials are reused per subtask, so C353-002-001 and C353-003-001 are
    different clips and must not be grouped together.
    """
    wanted_cam = _camera_token(camera)
    wanted_serial = int(serial)
    wanted_old = str(old_id).zfill(3) if old_id else ""
    found: list[Path] = []
    if not wanted_cam:
        return found
    for folder in _clip_search_dirs(task_dir, manifest):
        if not folder.is_dir():
            continue
        folder_id = _folder_subtask_id(folder.name) or ""
        for path in folder.iterdir():
            parsed = CLIP_NAME_RE.match(path.name)
            if parsed is None:
                continue
            if parsed.group("camera").upper() != wanted_cam:
                continue
            if int(parsed.group("clip")) != wanted_serial:
                continue
            file_id = parsed.group("subtask")
            if wanted_old and file_id == wanted_old:
                found.append(path)
            elif folder_id and file_id != folder_id:
                found.append(path)
    return found


def _delete_identity_copies(
    task_dir: Path,
    camera: str,
    serial: int,
    keep: Path | None,
    manifest: dict,
    *,
    old_id: str | None = None,
) -> None:
    keep_key = keep.resolve() if keep is not None and keep.is_file() else None
    if keep_key is None:
        return
    for path in _iter_identity_files(
        task_dir, camera, serial, manifest, old_id=old_id
    ):
        if path.resolve() == keep_key:
            continue
        path.unlink(missing_ok=True)


def _delete_named_clip_copies(
    task_dir: Path, filename: str, keep: Path | None, manifest: dict
) -> None:
    name = str(filename or "").strip()
    if not name:
        return
    keep_key = keep.resolve() if keep is not None and keep.is_file() else None
    for folder in _clip_search_dirs(task_dir, manifest):
        candidate = folder / name
        if not candidate.is_file():
            continue
        if keep_key is not None and candidate.resolve() == keep_key:
            continue
        candidate.unlink(missing_ok=True)
    parsed = CLIP_NAME_RE.match(name)
    if parsed is not None:
        _delete_identity_copies(
            task_dir,
            parsed.group("camera"),
            int(parsed.group("clip")),
            keep,
            manifest,
            old_id=parsed.group("subtask"),
        )


def _find_clip_file(task_dir: Path, filename: str, manifest: dict) -> Path | None:
    name = str(filename or "").strip()
    if not name:
        return None
    exact = next(
        (
            folder / name
            for folder in _clip_search_dirs(task_dir, manifest)
            if (folder / name).is_file()
        ),
        None,
    )
    if exact is not None:
        return exact
    parsed = CLIP_NAME_RE.match(name)
    if parsed is None:
        return None
    matches = _iter_identity_files(
        task_dir,
        parsed.group("camera"),
        int(parsed.group("clip")),
        manifest,
        old_id=parsed.group("subtask"),
    )
    return matches[0] if matches else None


def _clip_rows_for_source_label(
    task_dir: Path,
    manifest: dict,
    source: Path,
    label: str,
    *,
    camera: str = "",
    used: set[str] | None = None,
) -> list[dict]:
    """Clips for this source video in a subtask folder, unused by other segments."""
    claimed = {name.lower() for name in (used or set()) if name}
    sub = _subtask_by_label(manifest, label)
    if sub is None:
        return []
    wanted_cam = _camera_token(camera)
    rows: list[dict] = []
    seen: set[str] = set()
    source_key = source.name.lower()
    for clip in sub.get("clips") or []:
        filename = str(clip.get("filename") or "").strip()
        if not filename or filename.lower() in claimed or filename.lower() in seen:
            continue
        clip_source = str(clip.get("source_video") or "").strip()
        if clip_source and clip_source.lower() != source_key:
            continue
        parsed = CLIP_NAME_RE.match(filename)
        clip_cam = _camera_token(clip.get("camera_serial")) or (
            parsed.group("camera").upper() if parsed else ""
        )
        if wanted_cam and clip_cam and clip_cam != wanted_cam:
            continue
        rows.append(clip)
        seen.add(filename.lower())
    folder = task_dir / str(sub.get("folder") or "")
    if folder.is_dir():
        for path in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if path.name.lower() in claimed or path.name.lower() in seen:
                continue
            parsed = CLIP_NAME_RE.match(path.name)
            if parsed is None:
                continue
            if wanted_cam and parsed.group("camera").upper() != wanted_cam:
                continue
            rows.append(
                {
                    "filename": path.name,
                    "source_video": source.name,
                    "camera_serial": parsed.group("camera"),
                    "video_serial": int(parsed.group("clip")),
                }
            )
            seen.add(path.name.lower())
    rows.sort(key=lambda row: int(row.get("video_serial") or 0) or 10**9)
    return rows


def _assign_clip_row(segment: dict, row: dict, subtask: dict | None) -> None:
    filename = str(row.get("filename") or "").strip()
    if not filename:
        return
    parsed = CLIP_NAME_RE.match(filename)
    segment["clip_filename"] = filename
    if subtask is not None:
        segment["clip_path"] = f"{subtask['folder']}/{filename}"
        segment["subtask_id"] = subtask["id"]
    if parsed:
        segment["clip_serial"] = int(parsed.group("clip"))
        segment["camera_serial"] = parsed.group("camera")


def _remove_clip_identity_from_subtasks(
    manifest: dict, camera: str, serial: int, *, old_id: str | None = None
) -> None:
    wanted_cam = _camera_token(camera)
    wanted_serial = int(serial)
    wanted_old = str(old_id).zfill(3) if old_id else ""
    for subtask in manifest.get("subtasks") or []:
        kept: list[dict] = []
        for clip in subtask.get("clips") or []:
            parsed = CLIP_NAME_RE.match(str(clip.get("filename") or ""))
            ident = _clip_identity_from_name(clip.get("filename"))
            if ident != (wanted_cam, wanted_serial):
                kept.append(clip)
                continue
            file_id = parsed.group("subtask") if parsed else ""
            if wanted_old and file_id == wanted_old:
                continue
            if file_id and file_id != str(subtask.get("id") or ""):
                continue
            kept.append(clip)
        if len(kept) != len(subtask.get("clips") or []):
            subtask["clips"] = kept
            subtask["total_clips"] = len(kept)


def _upsert_clip_row(subtask: dict, row: dict) -> None:
    filename = str(row.get("filename") or "").strip()
    parsed = CLIP_NAME_RE.match(filename)
    dest_id = str(subtask.get("id") or "")
    kept: list[dict] = []
    for clip in subtask.get("clips") or []:
        other_name = str(clip.get("filename") or "")
        if other_name.lower() == filename.lower():
            continue
        other = CLIP_NAME_RE.match(other_name)
        if parsed and other:
            same_clip = (
                other.group("camera").upper() == parsed.group("camera").upper()
                and int(other.group("clip")) == int(parsed.group("clip"))
            )
            if same_clip and (
                other.group("subtask") == parsed.group("subtask")
                or other.group("subtask") != dest_id
            ):
                continue
        kept.append(clip)
    kept.append(row)
    kept.sort(key=lambda clip: int(clip.get("video_serial") or 0))
    subtask["clips"] = kept
    subtask["total_clips"] = len(kept)


def _bind_missing_clip_filenames(
    source: Path, annotation: dict, manifest: dict
) -> bool:
    """Attach on-disk clip names to segments that were labeled before JSON stored them."""
    task_dir = task_directory(source)
    camera = _camera_token(
        annotation.get("camera_serial") or annotation.get("cl_number")
    )
    used = {
        str(segment.get("clip_filename") or "").strip().lower()
        for segment in annotation.get("segments") or []
        if str(segment.get("clip_filename") or "").strip()
    }
    by_label: dict[str, list[dict]] = {}
    for segment in annotation.get("segments") or []:
        if str(segment.get("type") or "").lower() != "subtask":
            continue
        if str(segment.get("clip_filename") or "").strip():
            continue
        label = str(segment.get("label") or "").strip()
        if not label:
            continue
        by_label.setdefault(label, []).append(segment)
    changed = False
    for label, segments in by_label.items():
        segments.sort(
            key=lambda item: (float(item["start"]), float(item["end"]), str(item.get("id")))
        )
        candidates = _clip_rows_for_source_label(
            task_dir, manifest, source, label, camera=camera, used=used
        )
        if (
            not candidates
            and label.lower() != UNLABELED_TASK_LABEL.lower()
        ):
            candidates = _clip_rows_for_source_label(
                task_dir,
                manifest,
                source,
                UNLABELED_TASK_LABEL,
                camera=camera,
                used=used,
            )
        remaining = list(candidates)
        assigned: list[tuple[dict, dict]] = []
        unmatched: list[dict] = []
        for segment in segments:
            start = float(segment["start"])
            end = float(segment["end"])
            hit = None
            for row in remaining:
                row_start = _as_seconds(row.get("source_start"))
                row_end = _as_seconds(row.get("source_end"))
                if row_start is None:
                    continue
                if abs(row_start - start) <= 0.12 and (
                    row_end is None or abs(row_end - end) <= 0.12
                ):
                    hit = row
                    break
            if hit is not None:
                remaining.remove(hit)
                assigned.append((segment, hit))
            else:
                unmatched.append(segment)
        for segment, row in zip(unmatched, remaining):
            assigned.append((segment, row))
        subtask = _subtask_by_label(manifest, label)
        for segment, row in assigned:
            filename = str(row.get("filename") or "").strip()
            if not filename:
                continue
            _assign_clip_row(segment, row, subtask)
            used.add(filename.lower())
            changed = True
    return changed


def _resolve_clip_filename(
    source: Path,
    segment: dict,
    label: str,
    annotation: dict,
    manifest: dict,
) -> str:
    recorded = str(segment.get("clip_filename") or "").strip()
    if CLIP_NAME_RE.match(recorded):
        return recorded
    used = {
        str(item.get("clip_filename") or "").strip().lower()
        for item in annotation.get("segments") or []
        if item is not segment and str(item.get("clip_filename") or "").strip()
    }
    camera = _camera_token(
        segment.get("camera_serial")
        or annotation.get("camera_serial")
        or annotation.get("cl_number")
    )
    task_dir = task_directory(source)
    candidates = _clip_rows_for_source_label(
        task_dir, manifest, source, label, camera=camera, used=used
    )
    if not candidates and label.lower() != UNLABELED_TASK_LABEL.lower():
        candidates = _clip_rows_for_source_label(
            task_dir,
            manifest,
            source,
            UNLABELED_TASK_LABEL,
            camera=camera,
            used=used,
        )
    start = _as_seconds(segment.get("start"))
    end = _as_seconds(segment.get("end"))
    if start is not None:
        for row in candidates:
            row_start = _as_seconds(row.get("source_start"))
            row_end = _as_seconds(row.get("source_end"))
            if row_start is None:
                continue
            if abs(row_start - start) <= 0.12 and (
                row_end is None or end is None or abs(row_end - end) <= 0.12
            ):
                return str(row.get("filename") or "").strip()
    if candidates:
        return str(candidates[0].get("filename") or "").strip()
    return ""


def _json_claimed_clip_homes(
    task_dir: Path, manifest: dict
) -> tuple[dict[str, str], set[tuple[str, str, int]]]:
    """JSON is the source of truth for which file belongs in which subtask folder.

    Returns lowercase clip_filename → dest id, and (camera, dest id, serial)
    identities. Serials are reused across subtasks, so camera+serial alone is
    not a claim.
    """
    unlabeled_id = str(
        (_subtask_by_label(manifest, UNLABELED_TASK_LABEL) or {}).get("id") or ""
    )
    by_name: dict[str, str] = {}
    identities: set[tuple[str, str, int]] = set()
    for raw in _task_annotations(task_dir):
        fallback_cam = _camera_token(raw.get("camera_serial") or raw.get("cl_number"))
        for segment in raw.get("segments") or []:
            if str(segment.get("type") or "").lower() != "subtask":
                continue
            label = str(segment.get("label") or "").strip()
            if not label:
                continue
            sub = _subtask_by_label(manifest, label)
            if sub is None:
                continue
            dest_id = str(sub.get("id") or "")
            if not dest_id:
                continue
            filename = str(segment.get("clip_filename") or "").strip()
            if filename:
                key = filename.lower()
                previous = by_name.get(key)
                if previous is None or previous == unlabeled_id or dest_id != unlabeled_id:
                    by_name[key] = dest_id
            parsed = CLIP_NAME_RE.match(filename) if filename else None
            cam = (
                parsed.group("camera").upper()
                if parsed is not None
                else (_camera_token(segment.get("camera_serial")) or fallback_cam)
            )
            serial: int | None = None
            if parsed is not None:
                serial = int(parsed.group("clip"))
            else:
                raw_serial = segment.get("clip_serial")
                if raw_serial not in (None, ""):
                    try:
                        serial = int(raw_serial)
                    except (TypeError, ValueError):
                        serial = None
            if cam and serial is not None:
                identities.add((cam, dest_id, serial))
    return by_name, identities


def json_clip_names_for_subtask(task_dir: Path, subtask_id: str) -> set[str]:
    """Clip filenames already recorded in JSON for one subtask (any video)."""
    wanted = str(subtask_id or "").zfill(3)
    names: set[str] = set()
    if not wanted:
        return names
    for raw in _task_annotations(task_dir):
        for segment in raw.get("segments") or []:
            filename = str(segment.get("clip_filename") or "").strip()
            parsed = CLIP_NAME_RE.match(filename)
            if parsed is not None and parsed.group("subtask") == wanted:
                names.add(filename)
    return names


def _subtask_counts_by_camera(task_dir: Path, label: str) -> dict[str, int]:
    """How many labeled segments each camera has for this subtask."""
    wanted = str(label or "").strip().lower()
    counts: dict[str, int] = {}
    if not wanted:
        return counts
    for video in source_videos_in_task(task_dir):
        sidecar = sidecar_path_for(video)
        if not sidecar.is_file():
            continue
        try:
            raw = _read_json(sidecar)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        camera = _camera_token(raw.get("camera_serial") or raw.get("cl_number"))
        if not camera:
            continue
        n = 0
        for segment in raw.get("segments") or []:
            if str(segment.get("type") or "").lower() != "subtask":
                continue
            if str(segment.get("label") or "").strip().lower() == wanted:
                n += 1
        if n:
            counts[camera] = counts.get(camera, 0) + n
    return counts


def serials_reserved_by_earlier_videos(source: Path, label: str) -> set[int]:
    """Serials the previous source videos in this folder already own.

    Video 1 with 35 applying-sticker labels reserves 001–035 even if some
    files are still missing, so video 2 starts at 036.
    """
    held: set[int] = set()
    wanted = str(label or "").strip().lower()
    source = Path(source).expanduser().resolve()
    task_dir = task_directory(source)
    for video in source_videos_in_task(task_dir):
        if video.resolve() == source:
            break
        sidecar = sidecar_path_for(video)
        if not sidecar.is_file():
            continue
        try:
            raw = _read_json(sidecar)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        n = 0
        for segment in raw.get("segments") or []:
            if str(segment.get("type") or "").lower() != "subtask":
                continue
            if str(segment.get("label") or "").strip().lower() != wanted:
                continue
            n += 1
            filename = str(segment.get("clip_filename") or "").strip()
            parsed = CLIP_NAME_RE.match(filename)
            if parsed is not None:
                held.add(int(parsed.group("clip")))
            else:
                raw_serial = segment.get("clip_serial")
                if raw_serial not in (None, ""):
                    try:
                        held.add(int(raw_serial))
                    except (TypeError, ValueError):
                        pass
        if n:
            held.update(range(1, n + 1))
    return held


def _clip_belongs_in_dest(
    path: Path,
    dest_id: str,
    by_name: dict[str, str],
) -> bool:
    return by_name.get(path.name.lower()) == dest_id


def _remove_clip_filename_from_subtasks(manifest: dict, filename: str) -> None:
    key = str(filename or "").strip().lower()
    if not key:
        return
    for subtask in manifest.get("subtasks") or []:
        clips = subtask.get("clips") or []
        kept = [
            clip
            for clip in clips
            if str(clip.get("filename") or "").strip().lower() != key
        ]
        if len(kept) != len(clips):
            subtask["clips"] = kept
            subtask["total_clips"] = len(kept)


def _clip_needs_rehome(
    task_dir: Path, segment: dict, manifest: dict
) -> tuple[bool, str]:
    if str(segment.get("type") or "").lower() != "subtask":
        return False, ""
    label = str(segment.get("label") or "").strip()
    new_sub = _subtask_by_label(manifest, label)
    if new_sub is None:
        return False, ""
    recorded = str(segment.get("clip_filename") or "").strip()
    parsed = CLIP_NAME_RE.match(recorded)
    old_label = label
    if parsed:
        old_sub = _subtask_by_id(manifest, parsed.group("subtask"))
        if old_sub is not None:
            old_label = str(old_sub.get("name") or label)
    path = _find_clip_file(task_dir, recorded, manifest) if recorded else None
    wrong_id = bool(parsed and parsed.group("subtask") != new_sub["id"])
    wrong_folder = bool(
        path is not None and path.parent.name != str(new_sub.get("folder") or "")
    )
    return (wrong_id or wrong_folder), old_label


def _sync_segment_clips(source: Path, annotation: dict) -> bool:
    """Write missing clip names into JSON and move files that belong to a new label."""
    task_dir = task_directory(source)
    manifest = load_manifest(task_dir, repair=False)
    bound = _bind_missing_clip_filenames(source, annotation, manifest)
    changed = bound
    for segment in annotation.get("segments") or []:
        needs_move, old_label = _clip_needs_rehome(task_dir, segment, manifest)
        if not needs_move:
            continue
        _rehome_segment_clip(
            source,
            segment,
            old_label,
            str(segment.get("label") or ""),
            annotation,
        )
        manifest = load_manifest(task_dir, repair=False)
        changed = True
    if changed:
        manifest = load_manifest(task_dir, repair=False)
        renamed = _compact_labeled_clip_serials(task_dir, manifest)
        _apply_renamed_clip_filenames(annotation, renamed, manifest)
    return changed


def _relocate_clip_to_unlabeled(
    task_dir: Path,
    path: Path,
    manifest: dict,
    dest_occupied: set[str],
) -> bool:
    """Move one file into Unlabeled-task, keeping camera + serial when free."""
    parsed = CLIP_NAME_RE.match(path.name)
    if parsed is None or not path.is_file():
        return False
    camera = parsed.group("camera").upper()
    serial = int(parsed.group("clip"))
    file_id = parsed.group("subtask")
    unlabeled = _ensure_unlabeled_subtask(manifest)
    unlabeled_id = str(unlabeled.get("id") or "")
    unlabeled_dir = Path(task_dir) / str(unlabeled.get("folder") or "")
    unlabeled_dir.mkdir(parents=True, exist_ok=True)
    preferred = f"{camera}-{unlabeled_id}-{serial:03d}.mp4"
    preferred_path = unlabeled_dir / preferred
    old_name = path.name
    if preferred_path.is_file() and preferred_path.resolve() != path.resolve():
        path.unlink(missing_ok=True)
        new_name = preferred
        new_path = preferred_path
    else:
        unlabeled_occupied = _occupied_clip_names(unlabeled_dir)
        unlabeled_occupied.discard(path.name.lower())
        new_name = retarget_clip_filename(
            path.name, unlabeled_id, occupied=unlabeled_occupied
        )
        new_path = unlabeled_dir / new_name
        if path.resolve() != new_path.resolve():
            if new_path.exists():
                path.unlink(missing_ok=True)
            else:
                path.replace(new_path)
    dest_occupied.discard(old_name.lower())
    old_row: dict | None = None
    for other in manifest.get("subtasks") or []:
        for clip in other.get("clips") or []:
            if str(clip.get("filename") or "").strip().lower() == old_name.lower():
                old_row = dict(clip)
                break
        if old_row is not None:
            break
    _remove_clip_filename_from_subtasks(manifest, old_name)
    _remove_clip_identity_from_subtasks(manifest, camera, serial, old_id=file_id)
    row = old_row or {
        "camera_serial": camera,
        "video_serial": serial,
        "filename": new_name,
        "source_video": "",
    }
    parsed_new = CLIP_NAME_RE.match(new_name)
    row["filename"] = new_name
    row["camera_serial"] = camera
    row["video_serial"] = int(parsed_new.group("clip")) if parsed_new else serial
    _upsert_clip_row(unlabeled, row)
    return True


def _restore_unlabeled_clips_from_labeled_folders(task_dir: Path, manifest: dict) -> bool:
    """Move leftovers out of labeled folders; keep in-flight dest-id clips.

    JSON-named clips stay. Unclaimed files that already use this folder's
    dest id stay while this camera still has fewer files than labels —
    Trim can write ``C353-002-020`` before JSON records that name. True
    extras and wrong-id files go to Unlabeled-task.
    """
    unlabeled = _subtask_by_label(manifest, UNLABELED_TASK_LABEL)
    unlabeled_id = str(unlabeled.get("id") or "") if unlabeled else ""
    by_name, _identities = _json_claimed_clip_homes(task_dir, manifest)
    changed = False
    for subtask in manifest.get("subtasks") or []:
        dest_id = str(subtask.get("id") or "")
        if not dest_id or dest_id == unlabeled_id:
            continue
        folder = Path(task_dir) / str(subtask.get("folder") or "")
        if not folder.is_dir():
            continue
        dest_occupied = _occupied_clip_names(
            folder,
            [str(row.get("filename") or "") for row in subtask.get("clips") or []],
        )
        files = []
        for path in folder.iterdir():
            if not path.is_file():
                continue
            lower = path.name.lower()
            if lower.endswith("-stitched.mp4") or ".partial." in lower:
                if ".partial." in lower:
                    path.unlink(missing_ok=True)
                    changed = True
                continue
            files.append(path)
        pending_unclaimed: list[Path] = []
        pending_wrong: list[Path] = []
        for path in files:
            if not path.is_file():
                continue
            parsed = CLIP_NAME_RE.match(path.name)
            if parsed is None:
                continue
            camera = parsed.group("camera").upper()
            serial = int(parsed.group("clip"))
            file_id = parsed.group("subtask")
            if _clip_belongs_in_dest(path, dest_id, by_name):
                if file_id == dest_id:
                    continue
                new_name = retarget_clip_filename(
                    path.name, dest_id, occupied=dest_occupied
                )
                new_path = folder / new_name
                if path.resolve() != new_path.resolve():
                    if new_path.exists():
                        path.unlink(missing_ok=True)
                    else:
                        path.replace(new_path)
                dest_occupied.add(new_name.lower())
                dest_occupied.discard(path.name.lower())
                keep = new_path if new_path.is_file() else None
                _delete_identity_copies(
                    task_dir,
                    camera,
                    serial,
                    keep,
                    manifest,
                    old_id=file_id,
                )
                old_row: dict | None = None
                for other in manifest.get("subtasks") or []:
                    for clip in other.get("clips") or []:
                        ident = _clip_identity_from_name(clip.get("filename"))
                        clip_parsed = CLIP_NAME_RE.match(str(clip.get("filename") or ""))
                        if ident == (camera, serial) and (
                            clip_parsed is None or clip_parsed.group("subtask") == file_id
                        ):
                            old_row = dict(clip)
                            break
                    if old_row is not None:
                        break
                _remove_clip_identity_from_subtasks(
                    manifest, camera, serial, old_id=file_id
                )
                row = old_row or {
                    "camera_serial": camera,
                    "video_serial": serial,
                    "filename": new_name,
                    "source_video": "",
                }
                parsed_new = CLIP_NAME_RE.match(new_name)
                row["filename"] = new_name
                row["camera_serial"] = camera
                row["video_serial"] = (
                    int(parsed_new.group("clip")) if parsed_new else serial
                )
                _upsert_clip_row(subtask, row)
                changed = True
                continue
            if file_id == dest_id:
                pending_unclaimed.append(path)
            else:
                pending_wrong.append(path)

        counts = _subtask_counts_by_camera(task_dir, str(subtask.get("name") or ""))
        claimed_by_camera: dict[str, int] = {}
        if folder.is_dir():
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                parsed = CLIP_NAME_RE.match(path.name)
                if parsed is None or parsed.group("subtask") != dest_id:
                    continue
                if not _clip_belongs_in_dest(path, dest_id, by_name):
                    continue
                camera = parsed.group("camera").upper()
                claimed_by_camera[camera] = claimed_by_camera.get(camera, 0) + 1
        pending_unclaimed.sort(
            key=lambda item: (
                int(CLIP_NAME_RE.match(item.name).group("clip")),
                item.name.lower(),
            )
        )
        kept_extra: dict[str, int] = {}

        def _needs_more(camera: str) -> bool:
            allowed = int(counts.get(camera, 0) or 0)
            have = claimed_by_camera.get(camera, 0) + kept_extra.get(camera, 0)
            return have < allowed

        for path in pending_unclaimed:
            if not path.is_file():
                continue
            parsed = CLIP_NAME_RE.match(path.name)
            if parsed is None:
                continue
            camera = parsed.group("camera").upper()
            if _needs_more(camera):
                kept_extra[camera] = kept_extra.get(camera, 0) + 1
                continue
            pending_wrong.append(path)

        still_wrong: list[Path] = []
        for path in pending_wrong:
            if not path.is_file():
                continue
            parsed = CLIP_NAME_RE.match(path.name)
            if parsed is None:
                still_wrong.append(path)
                continue
            camera = parsed.group("camera").upper()
            file_id = parsed.group("subtask")
            serial = int(parsed.group("clip"))
            old_name = path.name
            # Trim can write the next applying serial before JSON records
            # the name, or a race can stamp Unlabeled-task's id onto a file
            # that is still sitting in applying-sticker. While this camera
            # still needs files here, keep it in this folder.
            if not _needs_more(camera):
                still_wrong.append(path)
                continue
            if file_id != dest_id:
                new_name = retarget_clip_filename(
                    path.name, dest_id, occupied=dest_occupied
                )
                new_path = folder / new_name
                if path.resolve() != new_path.resolve():
                    if new_path.exists():
                        still_wrong.append(path)
                        continue
                    path.replace(new_path)
                    path = new_path
                dest_occupied.add(new_name.lower())
                dest_occupied.discard(old_name.lower())
                _remove_clip_identity_from_subtasks(
                    manifest, camera, serial, old_id=file_id
                )
                _upsert_clip_row(
                    subtask,
                    {
                        "camera_serial": camera,
                        "video_serial": int(
                            CLIP_NAME_RE.match(path.name).group("clip")
                        )
                        if CLIP_NAME_RE.match(path.name)
                        else serial,
                        "filename": path.name,
                        "source_video": "",
                    },
                )
                changed = True
            kept_extra[camera] = kept_extra.get(camera, 0) + 1

        for path in still_wrong:
            if _relocate_clip_to_unlabeled(task_dir, path, manifest, dest_occupied):
                changed = True
    return changed


def _reclaim_labeled_clips_from_unlabeled(task_dir: Path, manifest: dict) -> bool:
    """Move JSON-named labeled clips out of Unlabeled-task back to their folder.

    Trim can write ``C353-002-020``, then a concurrent repair retargets it to
    ``C353-008-020`` before JSON records the applying name. Once JSON names
    ``C353-002-020``, put that file back without re-encoding.
    """
    unlabeled = _subtask_by_label(manifest, UNLABELED_TASK_LABEL)
    unlabeled_id = str(unlabeled.get("id") or "") if unlabeled else ""
    unlabeled_dir = Path(task_dir) / str((unlabeled or {}).get("folder") or "")
    if not unlabeled_id or not unlabeled_dir.is_dir():
        return False
    by_name, identities = _json_claimed_clip_homes(task_dir, manifest)
    unlabeled_index: dict[tuple[str, int], Path] = {}
    for path in unlabeled_dir.iterdir():
        if not path.is_file():
            continue
        parsed = CLIP_NAME_RE.match(path.name)
        if parsed is None or parsed.group("subtask") != unlabeled_id:
            continue
        unlabeled_index[
            (parsed.group("camera").upper(), int(parsed.group("clip")))
        ] = path
    changed = False
    for filename, dest_id in by_name.items():
        if not dest_id or dest_id == unlabeled_id:
            continue
        parsed = CLIP_NAME_RE.match(filename)
        if parsed is None:
            continue
        camera = parsed.group("camera").upper()
        serial = int(parsed.group("clip"))
        if parsed.group("subtask") != dest_id:
            continue
        if (camera, unlabeled_id, serial) in identities:
            continue
        dest_sub = _subtask_by_id(manifest, dest_id)
        if dest_sub is None:
            continue
        dest_dir = Path(task_dir) / str(dest_sub.get("folder") or "")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_name = f"{camera}-{dest_id}-{serial:03d}.mp4"
        dest_path = dest_dir / dest_name
        if dest_path.is_file():
            continue
        src = unlabeled_index.get((camera, serial))
        if src is None or not src.is_file():
            continue
        old_name = src.name
        src.replace(dest_path)
        unlabeled_index.pop((camera, serial), None)
        _remove_clip_filename_from_subtasks(manifest, old_name)
        _remove_clip_identity_from_subtasks(
            manifest, camera, serial, old_id=unlabeled_id
        )
        _upsert_clip_row(
            dest_sub,
            {
                "camera_serial": camera,
                "video_serial": serial,
                "filename": dest_name,
                "source_video": "",
            },
        )
        changed = True
    return changed


def _rewrite_json_clip_filenames(task_dir: Path, renamed: dict[str, str], manifest: dict) -> bool:
    if not renamed:
        return False
    changed = False
    folder_by_id = {
        str(row.get("id") or ""): str(row.get("folder") or "")
        for row in manifest.get("subtasks") or []
    }
    for video in source_videos_in_task(task_dir):
        sidecar = sidecar_path_for(video)
        if not sidecar.is_file():
            continue
        try:
            raw = _read_json(sidecar)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        wrote = False
        for segment in raw.get("segments") or []:
            name = str(segment.get("clip_filename") or "").strip()
            new_name = renamed.get(name.lower())
            if not new_name:
                continue
            segment["clip_filename"] = new_name
            parsed = CLIP_NAME_RE.match(new_name)
            if parsed is not None:
                segment["clip_serial"] = int(parsed.group("clip"))
                segment["subtask_id"] = parsed.group("subtask")
                folder = folder_by_id.get(parsed.group("subtask")) or ""
                if folder:
                    segment["clip_path"] = f"{folder}/{new_name}"
            wrote = True
        if wrote:
            _atomic_write(sidecar, raw)
            changed = True
    return changed


def _camera_rank_in_task(task_dir: Path) -> dict[str, int]:
    """Source-video order so video 1 keeps 001… and video 2 continues after."""
    ranks: dict[str, int] = {}
    for index, video in enumerate(source_videos_in_task(task_dir)):
        sidecar = sidecar_path_for(video)
        if not sidecar.is_file():
            continue
        try:
            raw = _read_json(sidecar)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        camera = _camera_token(raw.get("camera_serial") or raw.get("cl_number"))
        if camera and camera not in ranks:
            ranks[camera] = index
    return ranks


def _compact_labeled_clip_serials(task_dir: Path, manifest: dict) -> dict[str, str]:
    """Renumber a labeled folder so serials are unique: video 1, then video 2.

    Camera 1 with 35 labels keeps 001–035 even if some files are still
    missing. Camera 2 starts at 036. Files are copied through a local temp
    so a collision never deletes a clip.
    """
    unlabeled_id = str(
        (_subtask_by_label(manifest, UNLABELED_TASK_LABEL) or {}).get("id") or ""
    )
    renamed: dict[str, str] = {}
    ranks = _camera_rank_in_task(task_dir)
    for subtask in manifest.get("subtasks") or []:
        dest_id = str(subtask.get("id") or "")
        if not dest_id or dest_id == unlabeled_id:
            continue
        folder = Path(task_dir) / str(subtask.get("folder") or "")
        if not folder.is_dir():
            continue
        folder_files = [path for path in folder.iterdir() if path.is_file()]
        if any(".partial." in path.name.lower() for path in folder_files):
            continue
        by_camera: dict[str, list[Path]] = {}
        for path in folder_files:
            parsed = CLIP_NAME_RE.match(path.name)
            if parsed is None or parsed.group("subtask") != dest_id:
                continue
            camera = parsed.group("camera").upper()
            by_camera.setdefault(camera, []).append(path)
        for camera, paths in by_camera.items():
            paths.sort(
                key=lambda path: (
                    int(CLIP_NAME_RE.match(path.name).group("clip")),
                    path.name.lower(),
                )
            )
        counts = _subtask_counts_by_camera(task_dir, str(subtask.get("name") or ""))
        cameras = sorted(
            set(by_camera) | set(counts),
            key=lambda camera: (ranks.get(camera, 10_000), camera),
        )
        planned: list[tuple[Path, str]] = []
        next_serial = 1
        for camera in cameras:
            files = by_camera.get(camera, [])
            slots = max(len(files), int(counts.get(camera, 0) or 0))
            if slots <= 0:
                continue
            for offset, path in enumerate(files):
                new_name = f"{camera}-{dest_id}-{next_serial + offset:03d}.mp4"
                planned.append((path, new_name))
            next_serial += slots
        if not planned:
            continue
        dest_names = [name.lower() for _path, name in planned]
        if len(dest_names) != len(set(dest_names)):
            continue
        if all(path.name.lower() == name.lower() for path, name in planned):
            continue
        renamed.update(_rename_clips_via_local_temp(folder, planned))
    if renamed:
        _rewrite_json_clip_filenames(task_dir, renamed, manifest)
    return renamed


def _rename_clips_via_local_temp(
    folder: Path, planned: list[tuple[Path, str]]
) -> dict[str, str]:
    """Copy clips through %TEMP% so a name collision never drops a file."""
    moving = [
        (path, new_name)
        for path, new_name in planned
        if path.name.lower() != new_name.lower()
    ]
    if not moving:
        return {}
    tmp_root = Path(tempfile.gettempdir()) / "gopro-cleaner-renum"
    tmp_root.mkdir(parents=True, exist_ok=True)
    copies: list[tuple[Path, Path, Path, str]] = []
    renamed: dict[str, str] = {}
    try:
        for src, new_name in moving:
            tmp = tmp_root / f"{uuid.uuid4().hex}{src.suffix or '.mp4'}"
            shutil.copy2(src, tmp)
            copies.append((tmp, src, folder / new_name, src.name))
        moving_src = {src.resolve() for _tmp, src, _dest, _old in copies}
        if any(
            dest.exists() and dest.resolve() not in moving_src
            for _tmp, _src, dest, _old in copies
        ):
            return {}
        deleted: list[tuple[Path, Path]] = []
        try:
            for tmp, src, _dest, _old in copies:
                src.unlink(missing_ok=True)
                deleted.append((src, tmp))
            for tmp, _src, dest, old_name in copies:
                if dest.exists():
                    dest.unlink(missing_ok=True)
                shutil.copy2(tmp, dest)
                renamed[old_name.lower()] = dest.name
        except OSError:
            for src, tmp in deleted:
                if not src.exists() and tmp.is_file():
                    shutil.copy2(tmp, src)
            return {}
    finally:
        for tmp, _src, _dest, _old in copies:
            tmp.unlink(missing_ok=True)
    return renamed


def _apply_renamed_clip_filenames(
    annotation: dict, renamed: dict[str, str], manifest: dict
) -> None:
    if not renamed:
        return
    folder_by_id = {
        str(row.get("id") or ""): str(row.get("folder") or "")
        for row in manifest.get("subtasks") or []
    }
    for segment in annotation.get("segments") or []:
        name = str(segment.get("clip_filename") or "").strip()
        new_name = renamed.get(name.lower())
        if not new_name:
            continue
        segment["clip_filename"] = new_name
        parsed = CLIP_NAME_RE.match(new_name)
        if parsed is None:
            continue
        segment["clip_serial"] = int(parsed.group("clip"))
        segment["subtask_id"] = parsed.group("subtask")
        folder = folder_by_id.get(parsed.group("subtask")) or ""
        if folder:
            segment["clip_path"] = f"{folder}/{new_name}"


def _iter_named_clip_files(task_dir: Path, manifest: dict) -> list[Path]:
    files: list[Path] = []
    for folder in _clip_search_dirs(task_dir, manifest):
        if folder.resolve() == Path(task_dir).resolve() or not folder.is_dir():
            continue
        for path in folder.iterdir():
            if path.is_file() and CLIP_NAME_RE.match(path.name):
                files.append(path)
    return files


def _dedupe_misplaced_clip_copies(task_dir: Path, manifest: dict) -> bool:
    """Delete extra copies of the same filename. Different subtask IDs stay separate."""
    changed = False
    groups: dict[str, list[Path]] = {}
    for path in _iter_named_clip_files(task_dir, manifest):
        groups.setdefault(path.name.lower(), []).append(path)
    unlabeled = _subtask_by_label(manifest, UNLABELED_TASK_LABEL)
    unlabeled_id = str(unlabeled.get("id") or "") if unlabeled else ""
    for paths in groups.values():
        if len(paths) < 2:
            continue

        def _keep_score(path: Path) -> tuple[int, int, int]:
            parsed = CLIP_NAME_RE.match(path.name)
            file_id = parsed.group("subtask") if parsed else ""
            parent_id = _folder_subtask_id(path.parent.name) or ""
            id_matches_folder = 1 if parent_id == file_id else 0
            unlabeled_home = (
                1 if file_id == unlabeled_id and parent_id == unlabeled_id else 0
            )
            return (id_matches_folder, unlabeled_home, -len(str(path)))

        paths.sort(key=_keep_score, reverse=True)
        keep = paths[0]
        for extra in paths[1:]:
            if extra.is_file() and extra.resolve() != keep.resolve():
                extra.unlink(missing_ok=True)
                changed = True
    return changed


def _json_clip_file_meta(
    task_dir: Path,
) -> tuple[dict[str, dict], dict[tuple[str, int], dict], dict[str, str]]:
    """Map clip filename / (camera, serial) to source times from video JSON."""
    by_name: dict[str, dict] = {}
    by_ident: dict[tuple[str, int], dict] = {}
    camera_source: dict[str, str] = {}
    for raw in _task_annotations(task_dir):
        source_video = str(raw.get("source_video") or "").strip()
        fallback_cam = _camera_token(raw.get("camera_serial") or raw.get("cl_number"))
        if fallback_cam and source_video:
            camera_source[fallback_cam] = source_video
        for segment in raw.get("segments") or []:
            if str(segment.get("type") or "").lower() != "subtask":
                continue
            filename = str(segment.get("clip_filename") or "").strip()
            start = _as_seconds(segment.get("start"))
            end = _as_seconds(segment.get("end"))
            duration = _as_seconds(segment.get("duration"))
            if duration is None and start is not None and end is not None:
                duration = round(end - start, 3)
            cam = _camera_token(segment.get("camera_serial")) or fallback_cam
            parsed = CLIP_NAME_RE.match(filename)
            if parsed:
                cam = parsed.group("camera").upper()
            meta = {
                "source_video": source_video,
                "camera_serial": cam,
                "source_start": start,
                "source_end": end,
                "duration_seconds": duration,
                "filename": filename,
                "subtask_id": parsed.group("subtask") if parsed else "",
            }
            if filename:
                by_name[filename.lower()] = meta
            if parsed:
                by_ident[(parsed.group("camera").upper(), int(parsed.group("clip")))] = meta
    return by_name, by_ident, camera_source


def _rebuild_manifest_clips_from_disk(task_dir: Path, manifest: dict) -> bool:
    """Clip rows follow files on disk. Drop stale names; attach JSON times when present."""
    by_name, by_ident, camera_source = _json_clip_file_meta(task_dir)
    changed = False
    for subtask in manifest.get("subtasks") or []:
        folder = Path(task_dir) / str(subtask.get("folder") or "")
        old_by_name = {
            str(clip.get("filename") or "").strip().lower(): dict(clip)
            for clip in subtask.get("clips") or []
            if str(clip.get("filename") or "").strip()
        }
        rows: list[dict] = []
        if folder.is_dir():
            for path in sorted(folder.iterdir(), key=lambda item: item.name.lower()):
                if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                if path.name.lower().endswith("-stitched.mp4"):
                    continue
                parsed = CLIP_NAME_RE.match(path.name)
                if parsed is None:
                    continue
                camera = parsed.group("camera").upper()
                serial = int(parsed.group("clip"))
                old = old_by_name.get(path.name.lower()) or {}
                js = by_name.get(path.name.lower()) or by_ident.get((camera, serial)) or {}
                row = {
                    "camera_serial": camera,
                    "video_serial": serial,
                    "filename": path.name,
                    "source_video": str(
                        js.get("source_video")
                        or old.get("source_video")
                        or camera_source.get(camera)
                        or ""
                    ),
                }
                for key in ("duration_seconds", "source_start", "source_end"):
                    value = js.get(key)
                    if value is None:
                        value = old.get(key)
                    if value is not None:
                        row[key] = value
                rows.append(row)
        dest_id = str(subtask.get("id") or "")
        seen = {str(row.get("filename") or "").lower() for row in rows}
        for js in by_name.values():
            filename = str(js.get("filename") or "").strip()
            if not filename or filename.lower() in seen:
                continue
            if str(js.get("subtask_id") or "") != dest_id:
                continue
            parsed = CLIP_NAME_RE.match(filename)
            if parsed is None:
                continue
            row = {
                "camera_serial": parsed.group("camera").upper(),
                "video_serial": int(parsed.group("clip")),
                "filename": filename,
                "source_video": str(js.get("source_video") or ""),
            }
            for key in ("duration_seconds", "source_start", "source_end"):
                value = js.get(key)
                if value is not None:
                    row[key] = value
            rows.append(row)
            seen.add(filename.lower())
        rows.sort(key=lambda item: int(item.get("video_serial") or 0))
        old_names = [str(clip.get("filename") or "") for clip in subtask.get("clips") or []]
        new_names = [clip["filename"] for clip in rows]
        if old_names != new_names or int(subtask.get("total_clips") or 0) != len(rows):
            changed = True
        subtask["clips"] = rows
        subtask["total_clips"] = len(rows)
    return changed


def _rehome_segment_clip(
    source: Path,
    segment: dict,
    old_label: str,
    new_label: str,
    annotation: dict | None = None,
) -> None:
    """Move a trimmed clip when its segment is assigned a different subtask."""
    task_dir = task_directory(source)
    manifest = load_manifest(task_dir, repair=False)
    rows = annotation if annotation is not None else {"segments": [segment]}
    recorded = _resolve_clip_filename(source, segment, old_label, rows, manifest)
    match = CLIP_NAME_RE.match(recorded)
    new_sub = _subtask_by_label(manifest, new_label)
    old_sub = _subtask_by_label(manifest, old_label)
    if new_sub is None:
        return
    if not match:
        segment["subtask_id"] = new_sub["id"]
        return
    new_dir = task_dir / new_sub["folder"]
    new_dir.mkdir(parents=True, exist_ok=True)
    occupied = _occupied_clip_names(
        new_dir,
        [str(clip.get("filename") or "") for clip in new_sub.get("clips") or []]
        + list(json_clip_names_for_subtask(task_dir, new_sub["id"])),
    )
    new_name = retarget_clip_filename(recorded, new_sub["id"], occupied=occupied)
    new_path = new_dir / new_name
    old_path = _find_clip_file(task_dir, recorded, manifest)
    if old_path is None:
        old_path = _find_clip_file(task_dir, new_name, manifest)
    if old_path is None:
        identity_hits = _iter_identity_files(
            task_dir,
            match.group("camera"),
            int(match.group("clip")),
            manifest,
            old_id=match.group("subtask"),
        )
        old_path = identity_hits[0] if identity_hits else None
    if old_path is not None and old_path.resolve() != new_path.resolve():
        if not new_path.exists():
            old_path.replace(new_path)
        else:
            old_path.unlink(missing_ok=True)
    keep = new_path if new_path.is_file() else None
    _delete_named_clip_copies(task_dir, recorded, keep, manifest)
    if new_name.lower() != recorded.lower():
        _delete_named_clip_copies(task_dir, new_name, keep, manifest)
    _delete_identity_copies(
        task_dir,
        match.group("camera"),
        int(match.group("clip")),
        keep,
        manifest,
        old_id=match.group("subtask"),
    )

    parsed = CLIP_NAME_RE.match(new_name)
    segment["clip_filename"] = new_name
    segment["clip_path"] = f"{new_sub['folder']}/{new_name}"
    segment["subtask_id"] = new_sub["id"]
    if parsed:
        segment["clip_serial"] = int(parsed.group("clip"))
        segment["camera_serial"] = parsed.group("camera")

    old_key = recorded.lower()
    new_key = new_name.lower()
    serial = int(parsed.group("clip")) if parsed else int(match.group("clip"))
    camera = (parsed.group("camera") if parsed else match.group("camera")).upper()

    def _same_clip(clip: dict) -> bool:
        filename = str(clip.get("filename") or "").lower()
        if filename in {old_key, new_key}:
            return True
        parsed_clip = CLIP_NAME_RE.match(str(clip.get("filename") or ""))
        if parsed_clip is None:
            return False
        return (
            parsed_clip.group("camera").upper() == camera
            and int(parsed_clip.group("clip")) == serial
            and parsed_clip.group("subtask") == match.group("subtask")
        )

    if old_sub is not None and old_sub is not new_sub:
        old_sub["clips"] = [
            clip for clip in old_sub.get("clips") or [] if not _same_clip(clip)
        ]
        old_sub["total_clips"] = len(old_sub["clips"])
    clip_row = {
        "camera_serial": camera,
        "video_serial": serial,
        "filename": new_name,
        "source_video": source.name,
    }
    start = _as_seconds(segment.get("start"))
    end = _as_seconds(segment.get("end"))
    duration = _as_seconds(segment.get("duration"))
    if start is not None:
        clip_row["source_start"] = start
    if end is not None:
        clip_row["source_end"] = end
    if duration is not None:
        clip_row["duration_seconds"] = duration
    kept_rows = [
        clip for clip in new_sub.get("clips") or [] if not _same_clip(clip)
    ]
    by_name = {str(clip.get("filename") or ""): clip for clip in kept_rows}
    by_name[new_name] = {**by_name.get(new_name, {}), **clip_row}
    new_sub["clips"] = sorted(
        by_name.values(), key=lambda clip: int(clip.get("video_serial") or 0)
    )
    new_sub["total_clips"] = len(new_sub["clips"])
    _save_manifest(task_dir, manifest)


def _same_clip_camera(left: str, right: str) -> bool:
    left_parsed = CLIP_NAME_RE.match(str(left or "").strip())
    right_parsed = CLIP_NAME_RE.match(str(right or "").strip())
    if left_parsed is None or right_parsed is None:
        return False
    return left_parsed.group("camera").upper() == right_parsed.group("camera").upper()


def place_named_clip(
    source: Path, old_name: str, dest_dir: Path, new_name: str
) -> Path | None:
    """Move CAMERA-ID-SERIAL into dest_dir. Never delete another camera's clip."""
    task_dir = task_directory(source)
    manifest = load_manifest(task_dir, repair=False)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / new_name
    found = _find_clip_file(task_dir, old_name, manifest)
    if found is None:
        found = _find_clip_file(task_dir, new_name, manifest)
    keep: Path | None = None
    if found is not None and found.resolve() != target.resolve():
        if not target.exists():
            found.replace(target)
            keep = target if target.is_file() else None
        elif _same_clip_camera(found.name, new_name):
            found.unlink(missing_ok=True)
            keep = target if target.is_file() else None
        else:
            keep = found if found.is_file() else None
    elif target.is_file():
        keep = target
    elif found is not None and found.is_file():
        keep = found
    _delete_named_clip_copies(task_dir, old_name, keep, manifest)
    if (
        keep is not None
        and keep.is_file()
        and keep.resolve() == target.resolve()
        and str(new_name).lower() != str(old_name).lower()
    ):
        _delete_named_clip_copies(task_dir, new_name, keep, manifest)
    return keep if keep is not None and keep.is_file() else None


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
        clips = []
        for clip in item.get("clips") or []:
            if not isinstance(clip, dict):
                continue
            filename = str(clip.get("filename") or "").strip()
            if not filename:
                continue
            row = {
                "camera_serial": str(clip.get("camera_serial") or "UNKNOWN"),
                "video_serial": int(clip.get("video_serial") or 0),
                "filename": filename,
                "source_video": str(clip.get("source_video") or ""),
            }
            duration = _as_seconds(clip.get("duration_seconds", clip.get("duration")))
            if duration is not None:
                row["duration_seconds"] = duration
            start = _as_seconds(clip.get("source_start"))
            end = _as_seconds(clip.get("source_end"))
            if start is not None:
                row["source_start"] = start
            if end is not None:
                row["source_end"] = end
            clips.append(row)
        clips.sort(key=lambda clip: int(clip.get("video_serial") or 0))
        clip_seconds = sum(float(clip.get("duration_seconds") or 0.0) for clip in clips)
        duration_seconds = _as_seconds(item.get("duration_seconds", item.get("duration")))
        if duration_seconds is None:
            duration_seconds = round(clip_seconds, 3)
        stitched_duration = _as_seconds(
            item.get("stitched_duration_seconds", item.get("stitched_duration"))
        )
        subtask = {
            "id": subtask_id,
            "name": name,
            "folder": str(item.get("folder") or subtask_folder_name(name, subtask_id)),
            "total_clips": len(clips),
            "duration_seconds": duration_seconds,
            "clips": clips,
        }
        stitched_name = str(item.get("stitched_filename") or "").strip()
        if stitched_name:
            subtask["stitched_filename"] = stitched_name
        if stitched_duration is not None:
            subtask["stitched_duration_seconds"] = stitched_duration
        subtasks.append(subtask)
        seen_names.add(name.lower())
        used_ids.add(subtask_id)
    total_duration = round(sum(float(row.get("duration_seconds") or 0.0) for row in subtasks), 3)
    total_stitched = round(
        sum(float(row.get("stitched_duration_seconds") or 0.0) for row in subtasks),
        3,
    )
    return {
        "version": VERSION,
        "subtasks": subtasks,
        "total_duration_seconds": total_duration,
        "total_stitched_duration_seconds": total_stitched,
        "updated_at": str((raw or {}).get("updated_at") or _now_iso()),
    }


def _task_annotations(task_dir: Path) -> list[dict]:
    rows: list[dict] = []
    folder = Path(task_dir)
    legacy = folder / LEGACY_SEGMENT_FILE
    if legacy.is_file():
        rows.extend(_raw_annotation_rows(legacy))
    for video in source_videos_in_task(folder):
        sidecar = sidecar_path_for(video)
        if sidecar.is_file():
            rows.extend(_raw_annotation_rows(sidecar))
    return rows


def _migrate_clip_layout(task_dir: Path, manifest: dict) -> bool:
    """Move clips into stable subtask folders using CAMERA-ID-SERIAL names."""
    camera_by_source = {
        str(video.get("source_video") or "").lower(): str(
            video.get("camera_serial") or "UNKNOWN"
        )
        for video in _task_annotations(task_dir)
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
            parsed = CLIP_NAME_RE.match(old_name)
            if parsed:
                serial = int(parsed.group("clip"))
                camera = parsed.group("camera")
            if serial <= 0:
                continue
            occupied = _occupied_clip_names(
                target_dir,
                [str(row.get("filename") or "") for row in subtask.get("clips") or []],
            )
            new_name = retarget_clip_filename(
                old_name if parsed else f"{camera}-000-{serial:03d}.mp4",
                subtask["id"],
                occupied=occupied,
            )
            if not parsed:
                new_name = f"{camera}-{subtask['id']}-{serial:03d}.mp4"
                candidate = serial
                while new_name.lower() in occupied and new_name.lower() != old_name.lower():
                    candidate += 1
                    new_name = f"{camera}-{subtask['id']}-{candidate:03d}.mp4"
            new_path = target_dir / new_name
            candidates = [task_dir / old_name, target_dir / old_name]
            for other in manifest["subtasks"]:
                folder_name = str(other.get("folder") or "")
                if folder_name:
                    candidates.append(task_dir / folder_name / old_name)
            unlabeled_row = _subtask_by_label(manifest, UNLABELED_TASK_LABEL)
            unlabeled_folder = str((unlabeled_row or {}).get("folder") or "")
            unlabeled_sub_id = str((unlabeled_row or {}).get("id") or "")
            old_path = next((path for path in candidates if path.is_file()), None)
            if (
                old_path is not None
                and unlabeled_folder
                and old_path.parent.name == unlabeled_folder
                and str(subtask.get("id") or "") != unlabeled_sub_id
            ):
                # JSON-driven rehome moves unlabeled clips; do not steal them here.
                old_path = None
            if old_path is not None and old_path != new_path and not new_path.exists():
                old_path.replace(new_path)
            wrong_in_dest = target_dir / old_name
            if (
                old_name.lower() != new_name.lower()
                and wrong_in_dest.is_file()
                and wrong_in_dest.resolve() != new_path.resolve()
            ):
                wrong_in_dest.unlink(missing_ok=True)
            old_parsed = CLIP_NAME_RE.match(old_name)
            identity = _clip_identity_from_name(new_name) or _clip_identity_from_name(
                old_name
            )
            keep = new_path if new_path.is_file() else None
            if keep is None and identity is not None:
                hits = _iter_identity_files(
                    task_dir,
                    identity[0],
                    identity[1],
                    manifest,
                    old_id=old_parsed.group("subtask") if old_parsed else None,
                )
                keep = next(
                    (
                        path
                        for path in hits
                        if path.parent.resolve() == target_dir.resolve()
                    ),
                    hits[0] if hits else None,
                )
            if identity is not None and keep is not None:
                _delete_identity_copies(
                    task_dir,
                    identity[0],
                    identity[1],
                    keep,
                    manifest,
                    old_id=old_parsed.group("subtask") if old_parsed else None,
                )
            if old_name != new_name:
                clip["filename"] = new_name
                renamed[old_name] = (folder, new_name)
                changed = True
            if clip.get("camera_serial") != camera:
                clip["camera_serial"] = camera
                changed = True

        stitched_name = f"{folder}-stitched.mp4"
        stitched_target = task_dir / stitched_name
        nested_stitched = target_dir / stitched_name
        if nested_stitched.is_file() and (
            not stitched_target.exists()
            or nested_stitched.resolve() == stitched_target.resolve()
        ):
            if nested_stitched.resolve() != stitched_target.resolve():
                nested_stitched.replace(stitched_target)
                changed = True
        for old_stitched in (
            task_dir / f"{safe_label_name(subtask['name'])}-stitched.mp4",
            task_dir / f"{safe_label_name(subtask['name'])}__stitched.MP4",
        ):
            if (
                old_stitched.is_file()
                and old_stitched.resolve() != stitched_target.resolve()
                and not stitched_target.exists()
            ):
                old_stitched.replace(stitched_target)
                changed = True
                break

    if renamed:
        for video in source_videos_in_task(task_dir):
            sidecar = sidecar_path_for(video)
            if not sidecar.is_file():
                continue
            try:
                annotation = _read_json(sidecar)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(annotation, dict):
                continue
            updated = False
            for segment in annotation.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                old_name = str(segment.get("clip_filename") or "")
                if old_name in renamed:
                    folder, new_name = renamed[old_name]
                    segment["clip_filename"] = new_name
                    segment["clip_path"] = f"{folder}/{new_name}"
                    updated = True
            if updated:
                annotation["updated_at"] = _now_iso()
                _atomic_write(sidecar, annotation)
    return changed


def _labeled_seconds_by_subtask(task_dir: Path) -> dict[str, float]:
    """Sum labeled subtask time from each source video's JSON."""
    totals: dict[str, float] = {}
    for video in _task_annotations(task_dir):
        if not isinstance(video, dict):
            continue
        for segment in video.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            if str(segment.get("type") or "").lower() != "subtask":
                continue
            label = str(segment.get("label") or "").strip().lower()
            if not label:
                continue
            seconds = _as_seconds(segment.get("duration"))
            if seconds is None:
                start = _as_seconds(segment.get("start"))
                end = _as_seconds(segment.get("end"))
                if start is None or end is None or end <= start:
                    continue
                seconds = round(end - start, 3)
            totals[label] = round(totals.get(label, 0.0) + seconds, 3)
    return totals


def stitched_output_path(task_dir: Path, subtask: dict) -> Path:
    """Stitched MP4 sits in the main-task folder, next to manifest.json."""
    folder = str(
        subtask.get("folder") or subtask_folder_name(subtask["name"], subtask["id"])
    )
    return Path(task_dir) / f"{folder}-stitched.mp4"


def _stitched_path(task_dir: Path, subtask: dict) -> Path:
    preferred = stitched_output_path(task_dir, subtask)
    if preferred.is_file():
        return preferred
    folder = str(
        subtask.get("folder") or subtask_folder_name(subtask["name"], subtask["id"])
    )
    stored = str(subtask.get("stitched_filename") or "").strip()
    if stored:
        loose = Path(task_dir) / stored
        if loose.is_file():
            return loose
        nested = Path(task_dir) / folder / stored
        if nested.is_file():
            return nested
    nested_default = Path(task_dir) / folder / f"{folder}-stitched.mp4"
    if nested_default.is_file():
        return nested_default
    return preferred


def _attach_manifest_durations(task_dir: Path, manifest: dict) -> dict:
    labeled = _labeled_seconds_by_subtask(task_dir)
    for subtask in manifest.get("subtasks") or []:
        clip_seconds = round(
            sum(float(clip.get("duration_seconds") or 0.0) for clip in subtask.get("clips") or []),
            3,
        )
        labeled_seconds = labeled.get(str(subtask.get("name") or "").lower())
        subtask["duration_seconds"] = (
            labeled_seconds if labeled_seconds is not None else clip_seconds
        )
        stitched = _stitched_path(task_dir, subtask)
        if stitched.is_file():
            subtask["stitched_filename"] = stitched.name
            stored = _as_seconds(subtask.get("stitched_duration_seconds"))
            if stored is None:
                stored = _as_seconds(resolve_media_duration(stitched))
            if stored is not None:
                subtask["stitched_duration_seconds"] = stored
        elif "stitched_duration_seconds" in subtask and not stitched.is_file():
            subtask.pop("stitched_duration_seconds", None)
            subtask.pop("stitched_filename", None)
    manifest["total_duration_seconds"] = round(
        sum(float(row.get("duration_seconds") or 0.0) for row in manifest.get("subtasks") or []),
        3,
    )
    manifest["total_stitched_duration_seconds"] = round(
        sum(
            float(row.get("stitched_duration_seconds") or 0.0)
            for row in manifest.get("subtasks") or []
        ),
        3,
    )
    return manifest


def update_stitch_durations(source: Path, results: list[dict]) -> dict:
    """Record stitched-file duration for each subtask after a stitch run."""
    task_dir = export_directory(source)
    manifest = load_manifest(task_dir)
    by_name = {
        str(row.get("name") or "").strip().lower(): row for row in manifest["subtasks"]
    }
    for result in results:
        if not result.get("ok"):
            continue
        row = by_name.get(str(result.get("task") or "").strip().lower())
        if row is None:
            continue
        output = result.get("output")
        if output:
            path = Path(str(output))
            row["stitched_filename"] = path.name
        duration = _as_seconds(result.get("duration"))
        if duration is None and output:
            duration = _as_seconds(resolve_media_duration(Path(str(output))))
        if duration is not None:
            row["stitched_duration_seconds"] = duration
    return _save_manifest(task_dir, manifest)


def refresh_manifest_durations(task_dir: Path) -> dict:
    path = Path(task_dir)
    if not (path / MANIFEST_FILE).is_file():
        return _empty_manifest()
    return _save_manifest(path, load_manifest(path))


def load_manifest(path: Path, *, repair: bool = True) -> dict:
    manifest_path = Path(path)
    if manifest_path.is_dir():
        manifest_path = manifest_path / MANIFEST_FILE
    task_dir = manifest_path.parent
    if repair:
        cleanup_task_folder_files(task_dir)
    if not manifest_path.is_file():
        return _empty_manifest()
    try:
        raw = _read_json(manifest_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _empty_manifest()
    manifest = _normalize_manifest(raw if isinstance(raw, dict) else None)
    restored = reclaimed = changed = compacted = deduped = rebuilt = False
    if repair:
        restored = _restore_unlabeled_clips_from_labeled_folders(task_dir, manifest)
        reclaimed = _reclaim_labeled_clips_from_unlabeled(task_dir, manifest)
        compacted = bool(_compact_labeled_clip_serials(task_dir, manifest))
        changed = _migrate_clip_layout(task_dir, manifest)
        deduped = _dedupe_misplaced_clip_copies(task_dir, manifest)
        rebuilt = _rebuild_manifest_clips_from_disk(task_dir, manifest)
        _attach_manifest_durations(task_dir, manifest)
    raw_missing_durations = not (
        isinstance(raw, dict) and "total_duration_seconds" in raw
    )
    if (
        changed
        or restored
        or reclaimed
        or compacted
        or deduped
        or rebuilt
        or (repair and raw_missing_durations)
    ):
        manifest["updated_at"] = _now_iso()
        _atomic_write(manifest_path, manifest)
    return manifest


def _save_manifest(task_dir: Path, manifest: dict) -> dict:
    normalized = _normalize_manifest(manifest)
    normalized["updated_at"] = _now_iso()
    _attach_manifest_durations(task_dir, normalized)
    for subtask in normalized["subtasks"]:
        (Path(task_dir) / subtask["folder"]).mkdir(parents=True, exist_ok=True)
    _atomic_write(Path(task_dir) / MANIFEST_FILE, normalized)
    return normalized


def _ensure_manifest_from_annotation(
    source: Path, annotation: dict, *, repair: bool = True
) -> dict:
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
    manifest = load_manifest(task_dir, repair=repair)
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
    return [row["name"] for row in load_manifest(task_dir, repair=False)["subtasks"]]


def add_label(root: Path, parent_task: str, label: str) -> list[str]:
    task = parent_task.strip()
    clean = label.strip()
    if not task or not clean:
        return labels_for_task(root, task)
    with _lock:
        task_dir = _task_dir_for_name(root, task)
        if task_dir is None:
            raise ValueError(f"Could not find folder for main task: {task}")
        manifest = load_manifest(task_dir, repair=False)
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
    pending = {sidecar.parent.resolve() for sidecar in iter_sidecars(root)}
    pending.update(
        path.parent.resolve()
        for path in root.rglob(LEGACY_SEGMENT_FILE)
        if LABELING_DIR not in path.parts
    )
    pending.update(
        path.parent.resolve()
        for path in root.rglob(MANIFEST_FILE)
        if LABELING_DIR not in path.parts
    )
    for task_dir in pending:
        cleanup_task_folder_files(task_dir, root=root)

    for sidecar in iter_sidecars(root):
        rows = _raw_annotation_rows(sidecar)
        if not rows:
            continue
        parent_fallback = sidecar.parent.name
        try:
            doc = _read_json(sidecar)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            doc = {}
        if isinstance(doc, dict):
            parent_fallback = str(
                doc.get("parent_task") or doc.get("main_task") or parent_fallback
            ).strip() or parent_fallback
        for raw_annotation in rows:
            parent = str(raw_annotation.get("parent_task") or parent_fallback).strip()
            source = Path(str(raw_annotation.get("source_path") or "")).expanduser()
            if not source.is_file():
                source = sidecar.parent / str(raw_annotation.get("source_video") or "")
            if not source.is_file():
                matched = source_for_sidecar(sidecar)
                if matched is None:
                    continue
                source = matched
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
    manifest = load_manifest(manifest_path_for(source), repair=False)
    subtask = _subtask_by_label(manifest, label)
    if subtask is None:
        raise ValueError(f"Subtask is not defined in manifest.json: {label}")
    return export_directory(source) / subtask["folder"]


def subtask_id_for_label(source: Path, label: str) -> str:
    manifest = load_manifest(manifest_path_for(source), repair=False)
    subtask = _subtask_by_label(manifest, label)
    if subtask is None:
        raise ValueError(f"Subtask is not defined in manifest.json: {label}")
    return str(subtask["id"])


def clip_download_audit(source: Path) -> dict:
    """Compare this video's labeled subtasks to this camera's files on disk.

    Success means labeled count == downloaded count for every subtask:
    nothing missing, nothing extra. Other cameras in the same folder are
    ignored. Garbage segments are ignored.
    """
    source = Path(source).expanduser().resolve()
    annotation = load_annotation(source, repair=False)
    # Match next_clip_filename — trim writes UNKNOWN-* when metadata has no serial.
    camera = _camera_token(
        annotation.get("camera_serial") or annotation.get("cl_number") or "UNKNOWN"
    ) or "UNKNOWN"
    labeled_by: dict[str, int] = {}
    for segment in annotation.get("segments") or []:
        if str(segment.get("type") or "").lower() != "subtask":
            continue
        label = str(segment.get("label") or "").strip()
        if not label:
            continue
        labeled_by[label] = labeled_by.get(label, 0) + 1
    manifest = load_manifest(manifest_path_for(source), repair=False)
    task_dir = task_directory(source)
    labels = set(labeled_by)
    for subtask in manifest.get("subtasks") or []:
        name = str(subtask.get("name") or "").strip()
        if name:
            labels.add(name)
    rows: list[dict] = []
    labeled_total = downloaded_total = missing_total = extra_total = 0
    for label in sorted(labels, key=lambda item: item.lower()):
        labeled = int(labeled_by.get(label, 0))
        subtask = _subtask_by_label(manifest, label)
        folder = task_dir / str((subtask or {}).get("folder") or "")
        dest_id = str((subtask or {}).get("id") or "")
        downloaded = 0
        if camera and folder.is_dir():
            for path in folder.iterdir():
                if not path.is_file() or path.suffix.lower() not in {".mp4", ".mov"}:
                    continue
                if ".partial." in path.name.lower() or path.name.startswith("."):
                    continue
                parsed = CLIP_NAME_RE.match(path.name)
                if parsed is None:
                    continue
                if parsed.group("camera").upper() != camera:
                    continue
                if dest_id and parsed.group("subtask") != dest_id:
                    continue
                downloaded += 1
        if labeled == 0 and downloaded == 0:
            continue
        missing = max(0, labeled - downloaded)
        extra = max(0, downloaded - labeled)
        rows.append(
            {
                "label": label,
                "labeled": labeled,
                "downloaded": downloaded,
                "missing": missing,
                "extra": extra,
            }
        )
        labeled_total += labeled
        downloaded_total += downloaded
        missing_total += missing
        extra_total += extra
    return {
        "ok": bool(labeled_total > 0 and missing_total == 0 and extra_total == 0),
        "source_path": str(source),
        "source_name": source.name,
        "camera_serial": camera,
        "labeled": labeled_total,
        "downloaded": downloaded_total,
        "missing": missing_total,
        "extra": extra_total,
        "subtasks": rows,
    }


def next_clip_filename(
    source: Path,
    label: str,
    output_dir: Path,
    *,
    reserved: set[str] | None = None,
) -> str:
    """``CAMERASERIAL-SUBTASKID-CLIPSERIAL.mp4`` in the subtask folder."""
    annotation = load_annotation(source, repair=False)
    camera = re.sub(
        r"[^A-Za-z0-9]+",
        "",
        str(annotation.get("camera_serial") or annotation.get("cl_number") or "UNKNOWN"),
    ).upper() or "UNKNOWN"
    manifest = load_manifest(manifest_path_for(source), repair=False)
    subtask = next(
        (row for row in manifest["subtasks"] if row["name"].lower() == label.strip().lower()),
        None,
    )
    if subtask is None:
        raise ValueError(f"Subtask is not defined in manifest.json: {label}")
    prefix = f"{camera}-{subtask['id']}-"
    claimed = {name.lower() for name in (reserved or set())}
    if output_dir.is_dir():
        for path in output_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".mp4", ".mov"}:
                continue
            claimed.add(path.name.lower())
    # Only files on disk and this trim's reserved names. Ghost JSON names
    # from another video (C346-002-055 with no file) used to punch holes
    # so video 1 jumped from 019 to 077.
    used_serials = _serials_used_in_names(claimed)
    used_serials.update(serials_reserved_by_earlier_videos(source, label))
    candidate = 1
    while True:
        filename = f"{prefix}{candidate:03d}.mp4"
        if candidate not in used_serials and filename.lower() not in claimed:
            return filename
        candidate += 1


def list_subtask_export_dirs(source: Path) -> list[Path]:
    return [clips[0].parent for _subtask, clips in clips_by_subtask(source)]


def clips_by_subtask(source: Path) -> list[tuple[dict, list[Path]]]:
    task_dir = export_directory(source)
    manifest = load_manifest(task_dir, repair=False)
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
    manifest = load_manifest(task_dir, repair=False)
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
        duration = _as_seconds(row.get("duration_seconds", row.get("duration")))
        if duration is not None:
            clip["duration_seconds"] = duration
        start = _as_seconds(row.get("source_start"))
        end = _as_seconds(row.get("source_end"))
        if start is not None:
            clip["source_start"] = start
        if end is not None:
            clip["source_end"] = end
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

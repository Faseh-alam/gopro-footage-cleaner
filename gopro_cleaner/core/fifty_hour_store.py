"""Task-level JSON storage for the Scale AI 50-hour workflow.

Each source video owns ``{stem}.json`` (timestamps and camera metadata). Each
main-task folder also owns one ``manifest.json`` with stable subtask IDs and
generated short clips. Free-form subtask / garbage gaps are allowed.
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
LEGACY_SEGMENT_FILE = "segment.json"
SEGMENT_FILE = LEGACY_SEGMENT_FILE  # leftover combined task document
MANIFEST_FILE = "manifest.json"
SIDECAR_SUFFIX = ".json"
LABELING_DIR = "_labeling"
TASKS_FILE = "tasks.json"
PROGRESS_FILE = "progress.json"
VIDEO_EXTENSIONS = {".mp4", ".mov"}
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
            if path.suffix.lower() in {".json", ".txt"}:
                path.unlink(missing_ok=True)
            continue
        lower = path.name.lower()
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


def load_annotation(video: Path, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    sidecar = sidecar_path_for(source)
    with _lock:
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
        _ensure_manifest_from_annotation(source, annotation)
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
        if _sync_segment_clips(source, annotation):
            annotation["updated_at"] = _now_iso()
            _atomic_write(sidecar, annotation)
        return annotation


def save_annotation(video: Path, annotation: dict, *, root: Path | None = None) -> dict:
    source = Path(video).expanduser().resolve()
    normalized = normalize_annotation(annotation, source, root=root)
    normalized["updated_at"] = _now_iso()
    with _lock:
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
        annotation = load_annotation(source, root=root)
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
        save_annotation(source, annotation, root=root)
    if root is not None:
        if seg_type == "subtask":
            add_label(root, annotation["parent_task"], clean_label)
        refresh_progress(Path(root))
    refresh_manifest_durations(task_directory(source))
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
        if name == MANIFEST_FILE.lower():
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
) -> str:
    """Keep camera + clip serial; swap only the middle subtask id.

    ``C346-001-053.mp4`` + subtask ``003`` → ``C346-003-053.mp4``.
    If another camera already uses that serial in the same folder, bump
    (053 → 054, …) so numbers stay unique in the folder.
    """
    match = CLIP_NAME_RE.match(str(filename or "").strip())
    if not match:
        return str(filename or "").strip()
    target_id = str(subtask_id).zfill(3)
    camera = match.group("camera")
    serial = int(match.group("clip"))
    claimed = {name.lower() for name in (occupied or set())}
    claimed.discard(str(filename).lower())
    holders = _cameras_holding_serials(occupied or set())
    candidate = serial
    while True:
        name = f"{camera}-{target_id}-{candidate:03d}.mp4"
        others = holders.get(candidate, set()) - {camera.upper()}
        if name.lower() not in claimed and not others:
            return name
        candidate += 1


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


def clip_serial_taken_by_other_camera(filename: str, occupied: set[str]) -> bool:
    """True when another camera already uses this clip serial in the folder."""
    parsed = CLIP_NAME_RE.match(str(filename or "").strip())
    if parsed is None:
        return False
    others = _cameras_holding_serials(occupied).get(int(parsed.group("clip")), set())
    others.discard(parsed.group("camera").upper())
    return bool(others)


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


def _relabel_segments_from_clip_homes(
    source: Path, annotation: dict, manifest: dict
) -> bool:
    """If Unlabeled JSON still has clips sitting in a real subtask folder, adopt that label."""
    source_key = source.name.lower()
    camera = _camera_token(
        annotation.get("camera_serial") or annotation.get("cl_number")
    )
    unlabeled_key = UNLABELED_TASK_LABEL.lower()
    homes: list[tuple[dict, dict]] = []
    for subtask in manifest.get("subtasks") or []:
        if str(subtask.get("name") or "").strip().lower() == unlabeled_key:
            continue
        folder = task_directory(source) / str(subtask.get("folder") or "")
        for clip in subtask.get("clips") or []:
            clip_source = str(clip.get("source_video") or "").strip().lower()
            ident = _clip_identity_from_name(clip.get("filename"))
            clip_cam = _camera_token(clip.get("camera_serial")) or (
                ident[0] if ident else ""
            )
            if clip_source and clip_source != source_key:
                continue
            if not clip_source and camera and clip_cam and clip_cam != camera:
                continue
            if not clip_source and not camera:
                continue
            homes.append((subtask, clip))
        if folder.is_dir() and camera:
            for path in folder.iterdir():
                parsed = CLIP_NAME_RE.match(path.name)
                if parsed is None or parsed.group("camera").upper() != camera:
                    continue
                homes.append(
                    (
                        subtask,
                        {
                            "filename": path.name,
                            "camera_serial": parsed.group("camera"),
                            "video_serial": int(parsed.group("clip")),
                            "source_video": source.name,
                        },
                    )
                )
    unique_subs: list[dict] = []
    seen: set[str] = set()
    for subtask, _clip in homes:
        key = str(subtask.get("name") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique_subs.append(subtask)
    changed = False
    for segment in annotation.get("segments") or []:
        if str(segment.get("type") or "").lower() != "subtask":
            continue
        if str(segment.get("label") or "").strip().lower() != unlabeled_key:
            continue
        recorded = str(segment.get("clip_filename") or "").strip()
        ident = _clip_identity_from_name(recorded)
        start = _as_seconds(segment.get("start"))
        hit_sub: dict | None = None
        hit_clip: dict | None = None
        for subtask, clip in homes:
            clip_name = str(clip.get("filename") or "").strip()
            clip_ident = _clip_identity_from_name(clip_name)
            if recorded and clip_name.lower() == recorded.lower():
                hit_sub, hit_clip = subtask, clip
                break
            if ident is not None and clip_ident == ident:
                hit_sub, hit_clip = subtask, clip
                break
            row_start = _as_seconds(clip.get("source_start"))
            if (
                start is not None
                and row_start is not None
                and abs(row_start - start) <= 0.12
            ):
                hit_sub, hit_clip = subtask, clip
                break
        if hit_sub is None and len(unique_subs) == 1:
            hit_sub = unique_subs[0]
        if hit_sub is None:
            continue
        segment["label"] = str(hit_sub["name"])
        if hit_clip is not None:
            _assign_clip_row(segment, hit_clip, hit_sub)
        else:
            segment["subtask_id"] = hit_sub["id"]
        changed = True
    return changed


def _sync_segment_clips(source: Path, annotation: dict) -> bool:
    """Write missing clip names into JSON and move files that belong to a new label."""
    task_dir = task_directory(source)
    manifest = load_manifest(task_dir)
    changed = _relabel_segments_from_clip_homes(source, annotation, manifest)
    if changed:
        manifest = load_manifest(task_dir)
    bound = _bind_missing_clip_filenames(source, annotation, manifest)
    changed = changed or bound
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
        manifest = load_manifest(task_dir)
        changed = True
    return changed


def _adopt_wrong_id_clips_in_labeled_folders(task_dir: Path, manifest: dict) -> bool:
    """Rename CAMERA-001-SERIAL files that already live in a labeled subtask folder.

    Copies left in Unlabeled-task are deleted. Manifest rows follow the file.
    """
    unlabeled = _subtask_by_label(manifest, UNLABELED_TASK_LABEL)
    unlabeled_id = str(unlabeled.get("id") or "") if unlabeled else ""
    changed = False
    for subtask in manifest.get("subtasks") or []:
        dest_id = str(subtask.get("id") or "")
        if not dest_id or dest_id == unlabeled_id:
            continue
        folder = Path(task_dir) / str(subtask.get("folder") or "")
        if not folder.is_dir():
            continue
        occupied = _occupied_clip_names(
            folder,
            [str(row.get("filename") or "") for row in subtask.get("clips") or []],
        )
        for path in list(folder.iterdir()):
            if not path.is_file():
                continue
            parsed = CLIP_NAME_RE.match(path.name)
            if parsed is None or parsed.group("subtask") == dest_id:
                continue
            camera = parsed.group("camera").upper()
            serial = int(parsed.group("clip"))
            new_name = retarget_clip_filename(path.name, dest_id, occupied=occupied)
            new_path = folder / new_name
            if path.resolve() != new_path.resolve():
                if new_path.exists():
                    path.unlink(missing_ok=True)
                else:
                    path.replace(new_path)
            occupied.add(new_name.lower())
            occupied.discard(path.name.lower())
            keep = new_path if new_path.is_file() else None
            _delete_identity_copies(
                task_dir, camera, serial, keep, manifest, old_id=parsed.group("subtask")
            )
            old_row: dict | None = None
            old_id = parsed.group("subtask")
            for other in manifest.get("subtasks") or []:
                for clip in other.get("clips") or []:
                    ident = _clip_identity_from_name(clip.get("filename"))
                    clip_parsed = CLIP_NAME_RE.match(str(clip.get("filename") or ""))
                    if ident == (camera, serial) and (
                        clip_parsed is None or clip_parsed.group("subtask") == old_id
                    ):
                        old_row = dict(clip)
                        break
                if old_row is not None:
                    break
            _remove_clip_identity_from_subtasks(
                manifest, camera, serial, old_id=old_id
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
    return changed


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
            labeled_folder = 1 if parent_id and parent_id != unlabeled_id else 0
            id_matches_folder = 1 if parent_id == file_id else 0
            return (labeled_folder, id_matches_folder, -len(str(path)))

        paths.sort(key=_keep_score, reverse=True)
        keep = paths[0]
        for extra in paths[1:]:
            if extra.is_file() and extra.resolve() != keep.resolve():
                extra.unlink(missing_ok=True)
                changed = True
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
    manifest = load_manifest(task_dir)
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
        [str(clip.get("filename") or "") for clip in new_sub.get("clips") or []],
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


def place_named_clip(
    source: Path, old_name: str, dest_dir: Path, new_name: str
) -> Path | None:
    """Move CAMERA-ID-SERIAL into dest_dir and delete Unlabeled / duplicate copies."""
    task_dir = task_directory(source)
    manifest = load_manifest(task_dir)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / new_name
    found = _find_clip_file(task_dir, old_name, manifest)
    if found is None:
        found = _find_clip_file(task_dir, new_name, manifest)
    if found is not None and found.resolve() != target.resolve():
        if not target.exists():
            found.replace(target)
        else:
            found.unlink(missing_ok=True)
    keep = target if target.is_file() else None
    _delete_named_clip_copies(task_dir, old_name, keep, manifest)
    if str(new_name).lower() != str(old_name).lower():
        _delete_named_clip_copies(task_dir, new_name, keep, manifest)
    return keep


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
            old_path = next((path for path in candidates if path.is_file()), None)
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
        stitched_target = target_dir / stitched_name
        for old_stitched in (
            task_dir / f"{safe_label_name(subtask['name'])}-stitched.mp4",
            task_dir / f"{safe_label_name(subtask['name'])}__stitched.MP4",
        ):
            if old_stitched.is_file() and not stitched_target.exists():
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


def _stitched_path(task_dir: Path, subtask: dict) -> Path:
    folder = str(subtask.get("folder") or subtask_folder_name(subtask["name"], subtask["id"]))
    stored = str(subtask.get("stitched_filename") or "").strip()
    if stored:
        nested = Path(task_dir) / folder / stored
        if nested.is_file():
            return nested
        loose = Path(task_dir) / stored
        if loose.is_file():
            return loose
    return Path(task_dir) / folder / f"{folder}-stitched.mp4"


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
            row["folder"] = row.get("folder") or path.parent.name
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


def load_manifest(path: Path) -> dict:
    manifest_path = Path(path)
    if manifest_path.is_dir():
        manifest_path = manifest_path / MANIFEST_FILE
    task_dir = manifest_path.parent
    cleanup_task_folder_files(task_dir)
    if not manifest_path.is_file():
        return _empty_manifest()
    try:
        raw = _read_json(manifest_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _empty_manifest()
    manifest = _normalize_manifest(raw if isinstance(raw, dict) else None)
    adopted = _adopt_wrong_id_clips_in_labeled_folders(task_dir, manifest)
    changed = _migrate_clip_layout(task_dir, manifest)
    deduped = _dedupe_misplaced_clip_copies(task_dir, manifest)
    _attach_manifest_durations(task_dir, manifest)
    raw_missing_durations = not (
        isinstance(raw, dict) and "total_duration_seconds" in raw
    )
    if changed or adopted or deduped or raw_missing_durations:
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
    manifest = load_manifest(manifest_path_for(source))
    subtask = _subtask_by_label(manifest, label)
    if subtask is None:
        raise ValueError(f"Subtask is not defined in manifest.json: {label}")
    return export_directory(source) / subtask["folder"]


def subtask_id_for_label(source: Path, label: str) -> str:
    manifest = load_manifest(manifest_path_for(source))
    subtask = _subtask_by_label(manifest, label)
    if subtask is None:
        raise ValueError(f"Subtask is not defined in manifest.json: {label}")
    return str(subtask["id"])


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
    claimed = {name.lower() for name in (reserved or set())}
    if output_dir.is_dir():
        for path in output_dir.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".mp4", ".mov"}:
                continue
            claimed.add(path.name.lower())
    # Only count files that exist (plus this trim's reserved names). Stale
    # manifest rows from deleted clips used to skip 031–085 up to 086.
    holders = _cameras_holding_serials(claimed)
    used_serials = {
        int(match.group("clip"))
        for name in claimed
        if (match := CLIP_NAME_RE.match(name))
    }
    candidate = 1
    while True:
        filename = f"{prefix}{candidate:03d}.mp4"
        others = holders.get(candidate, set()) - {camera}
        if (
            candidate not in used_serials
            and filename.lower() not in claimed
            and not others
        ):
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

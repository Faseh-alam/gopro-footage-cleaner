"""Scan SD cards, clean raw footage, and label trimmed clips into task folders."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from .probe import probe_media
from .task_store import task_folder_name
from .timestamps import format_timestamp
from .trimmer import TrimJob, _execute_trim, build_output_path, job_store, move_to_trash

LABELED_FOLDER = "Labeled"
TRIMMED_SUFFIX_RE = re.compile(r"-\d+$", re.IGNORECASE)
CAMERA_FOLDER_RE = re.compile(r"^C\d{4}$", re.IGNORECASE)
SKIP_DIR_NAMES = {LABELED_FOLDER.lower(), "labeled", "tasks", ".trash"}


def _probe_duration(path: Path) -> float | None:
    try:
        return probe_media(path).duration
    except (RuntimeError, OSError):
        return None


def is_trimmed_clip(path: Path) -> bool:
    return path.suffix.upper() == ".MP4" and bool(TRIMMED_SUFFIX_RE.search(path.stem))


def is_raw_footage(path: Path) -> bool:
    if path.suffix.upper() != ".MP4" or path.name.startswith("._"):
        return False
    if is_trimmed_clip(path):
        return False
    for part in path.parts:
        if part.lower() in SKIP_DIR_NAMES:
            return False
    return True


def _video_dict(path: Path, root: Path) -> dict:
    duration = _probe_duration(path)
    try:
        relative = str(path.relative_to(root))
    except ValueError:
        relative = path.name
    return {
        "path": str(path.resolve()),
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "duration": duration,
        "duration_label": format_timestamp(duration) if duration else None,
        "relative": relative,
        "is_trimmed": is_trimmed_clip(path),
    }


def list_camera_folders(root: Path) -> list[dict]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    cameras: list[dict] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not CAMERA_FOLDER_RE.match(entry.name):
            continue
        raw_count = sum(1 for path in entry.rglob("*") if path.is_file() and is_raw_footage(path))
        clip_count = sum(1 for path in entry.rglob("*") if path.is_file() and is_trimmed_clip(path))
        cameras.append(
            {
                "name": entry.name,
                "path": str(entry.resolve()),
                "raw_count": raw_count,
                "clip_count": clip_count,
            }
        )
    return cameras


def scan_mp4_files(
    root: Path,
    *,
    recursive: bool = True,
    mode: str = "all",
) -> list[dict]:
    """mode: all | raw | clips"""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    candidates: list[Path] = []
    if recursive:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if mode == "raw" and is_raw_footage(path):
                candidates.append(path)
            elif mode == "clips" and is_trimmed_clip(path):
                candidates.append(path)
            elif mode == "all" and path.suffix.upper() == ".MP4" and not path.name.startswith("._"):
                candidates.append(path)
    else:
        for path in root.iterdir():
            if not path.is_file():
                continue
            if mode == "raw" and is_raw_footage(path):
                candidates.append(path)
            elif mode == "clips" and is_trimmed_clip(path):
                candidates.append(path)
            elif mode == "all" and path.suffix.upper() == ".MP4" and not path.name.startswith("._"):
                candidates.append(path)

    videos = [_video_dict(path, root) for path in sorted(candidates, key=lambda p: p.name.lower())]
    return videos


def _next_clip_number(source: Path) -> int:
    parent = source.parent
    stem = source.stem
    existing = 0
    for path in parent.iterdir():
        if not path.is_file() or path.suffix.upper() != ".MP4":
            continue
        if path.stem.startswith(f"{stem}-") and path.stem[len(stem) + 1 :].isdigit():
            existing = max(existing, int(path.stem.rsplit("-", 1)[-1]))
    return existing + 1


def trim_single_clip(source: Path, start_seconds: float, end_seconds: float) -> dict:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    if end_seconds <= start_seconds:
        raise ValueError("Clip end must be after start")

    clip_number = _next_clip_number(source)
    output_path = build_output_path(source, clip_number, source.parent)
    if output_path.exists():
        raise FileExistsError(f"Output already exists: {output_path.name}")

    job = TrimJob(
        job_id=str(uuid.uuid4()),
        input_path=source,
        output_path=output_path,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        clip_number=clip_number,
    )
    job_store.create(job)
    _execute_trim(job)
    finished = job_store.get(job.job_id)
    if finished is None or finished.status != "completed":
        error = finished.error if finished else "Trim failed"
        raise RuntimeError(error or "Trim failed")

    return {
        "output": str(output_path),
        "clip_number": clip_number,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
    }


def finish_cleaning_file(source: Path, *, delete_source: bool = True) -> dict:
    """Remove the raw file after all useful clips have been trimmed."""
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source}")

    deleted = False
    if delete_source and is_raw_footage(source):
        move_to_trash(source)
        deleted = True

    return {"deleted_source": deleted, "source": str(source)}


def assign_clip_to_task(clip_path: Path, label_root: Path, task_name: str) -> dict:
    """Move a trimmed clip into Labeled/<task>/ under label_root."""
    clip_path = clip_path.expanduser().resolve()
    label_root = label_root.expanduser().resolve()
    if not clip_path.exists():
        raise FileNotFoundError(f"Clip not found: {clip_path}")
    if not is_trimmed_clip(clip_path):
        raise ValueError("Only trimmed clips (filename-N.MP4) can be labeled")

    task_dir = label_root / LABELED_FOLDER / task_folder_name(task_name)
    task_dir.mkdir(parents=True, exist_ok=True)
    dest = task_dir / clip_path.name
    if dest.exists():
        raise FileExistsError(f"Already exists: {dest}")

    shutil.move(str(clip_path), str(dest))
    return {
        "task": task_name,
        "task_dir": str(task_dir),
        "output": str(dest),
        "moved": True,
    }


def process_reviewed_video(
    source: Path,
    output_root: Path,
    task_name: str,
    *,
    keep_entire: bool,
    clips: list[tuple[float, float]],
    delete_source: bool,
) -> dict:
    """Legacy combined clean + label in one step."""
    source = source.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source}")

    task_dir = output_root / task_folder_name(task_name)
    task_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []

    if keep_entire:
        dest = task_dir / source.name
        if dest.exists():
            raise FileExistsError(f"Already exists: {dest}")
        shutil.move(str(source), str(dest))
        outputs.append(str(dest))
    else:
        if not clips:
            raise ValueError("Add at least one useful clip or choose Keep entire file")
        for clip_number, (start_seconds, end_seconds) in enumerate(clips, 1):
            output_path = build_output_path(source, clip_number, task_dir)
            if output_path.exists():
                raise FileExistsError(f"Output already exists: {output_path.name}")
            job = TrimJob(
                job_id=str(uuid.uuid4()),
                input_path=source,
                output_path=output_path,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                clip_number=clip_number,
            )
            job_store.create(job)
            _execute_trim(job)
            finished = job_store.get(job.job_id)
            if finished is None or finished.status != "completed":
                error = finished.error if finished else "Trim failed"
                raise RuntimeError(error or "Trim failed")
            outputs.append(str(output_path))

    if delete_source:
        move_to_trash(source)

    return {
        "task": task_name,
        "task_dir": str(task_dir),
        "outputs": outputs,
        "deleted_source": delete_source,
    }

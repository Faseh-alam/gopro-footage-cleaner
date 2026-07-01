"""Scan SD cards and export reviewed footage into task folders."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from .probe import probe_media
from .task_store import task_folder_name
from .timestamps import format_timestamp
from .trimmer import TrimJob, _execute_trim, build_output_path, job_store, move_to_trash


def _probe_duration(path: Path) -> float | None:
    try:
        return probe_media(path).duration
    except (RuntimeError, OSError):
        return None


def scan_mp4_files(root: Path, recursive: bool = True) -> list[dict]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    candidates: list[Path] = []
    if recursive:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.upper() == ".MP4" and not path.name.startswith("._"):
                candidates.append(path)
    else:
        for path in root.iterdir():
            if path.is_file() and path.suffix.upper() == ".MP4" and not path.name.startswith("._"):
                candidates.append(path)

    videos: list[dict] = []
    for path in sorted(candidates, key=lambda p: p.name.lower()):
        duration = _probe_duration(path)
        videos.append(
            {
                "path": str(path.resolve()),
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "duration": duration,
                "duration_label": format_timestamp(duration) if duration else None,
                "relative": str(path.relative_to(root)),
            }
        )
    return videos


def process_reviewed_video(
    source: Path,
    output_root: Path,
    task_name: str,
    *,
    keep_entire: bool,
    clips: list[tuple[float, float]],
    delete_source: bool,
) -> dict:
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
        shutil.copy2(source, dest)
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

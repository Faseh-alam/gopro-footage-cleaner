"""Scan SD cards, clean raw footage, and label trimmed clips into task folders."""

from __future__ import annotations

import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .probe import probe_media
from .task_store import load_tasks, task_folder_name
from .timestamps import format_timestamp
from .trimmer import TrimJob, _execute_trim, build_output_path, clip_base_stem, job_store, move_to_trash

LABELED_FOLDER = "Labeled"  # legacy folder name — no longer created for new labels
TRIMMED_SUFFIX_RE = re.compile(r"-\d+$", re.IGNORECASE)
CAMERA_FOLDER_RE = re.compile(r"^C\d{4}$", re.IGNORECASE)
SKIP_DIR_NAMES = {LABELED_FOLDER.lower(), "labeled", "tasks", ".trash"}


def task_directory(label_root: Path, task_name: str) -> Path:
    """Task folder directly beside the footage (no Labeled/ wrapper)."""
    return label_root.expanduser().resolve() / task_folder_name(task_name)


def is_under_task_folder(path: Path, root: Path) -> bool:
    root = root.expanduser().resolve()
    try:
        rel = path.parent.relative_to(root)
    except ValueError:
        return False
    if not rel.parts:
        return False
    task_slugs = {task_folder_name(task) for task in load_tasks()}
    first = rel.parts[0]
    if CAMERA_FOLDER_RE.match(first):
        return len(rel.parts) > 1 and rel.parts[1] in task_slugs
    return first in task_slugs


def _footage_blocked(path: Path, root: Path | None = None) -> bool:
    for part in path.parts:
        if part.lower() in SKIP_DIR_NAMES:
            return True
    return bool(root and is_under_task_folder(path, root))


def _probe_duration(path: Path) -> float | None:
    try:
        return probe_media(path).duration
    except Exception as exc:  # noqa: BLE001
        # Surface missing ffmpeg clearly once; otherwise skip duration.
        from .ffmpeg_tools import FFmpegNotFoundError

        if isinstance(exc, FFmpegNotFoundError):
            raise
        return None


def is_hidden_or_temp_mp4(path: Path) -> bool:
    """True for Finder junk, in-progress trim temps (.name… / *.partial)."""
    name = path.name
    if name.startswith("."):
        return True
    lower = name.lower()
    return lower.endswith(".partial") or ".partial." in lower


def is_trimmed_clip(path: Path) -> bool:
    return (
        path.suffix.upper() == ".MP4"
        and not is_hidden_or_temp_mp4(path)
        and bool(TRIMMED_SUFFIX_RE.search(path.stem))
    )


def is_labelable_footage(path: Path, *, root: Path | None = None) -> bool:
    """Candidate for the label list (temps excluded; active trims filtered at scan)."""
    if path.suffix.upper() != ".MP4" or is_hidden_or_temp_mp4(path):
        return False
    return not _footage_blocked(path, root)


def is_raw_footage(path: Path, *, root: Path | None = None) -> bool:
    if path.suffix.upper() != ".MP4" or is_hidden_or_temp_mp4(path):
        return False
    if is_trimmed_clip(path):
        return False
    return not _footage_blocked(path, root)


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


def _iter_mp4_files(root: Path, *, recursive: bool = True):
    root = root.expanduser().resolve()
    if recursive:
        yield from (path for path in root.rglob("*") if path.is_file())
    else:
        yield from (path for path in root.iterdir() if path.is_file())


def label_progress(root: Path, *, recursive: bool = True) -> dict:
    """Count footage still outside task folders vs already labeled."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    unlabeled = 0
    labeled = 0
    skipped = 0
    unlabeled_names: list[str] = []

    for path in _iter_mp4_files(root, recursive=recursive):
        if path.suffix.upper() != ".MP4" or path.name.startswith("._"):
            continue
        if any(part.lower() in SKIP_DIR_NAMES for part in path.parts):
            skipped += 1
            continue
        if is_under_task_folder(path, root):
            labeled += 1
            continue
        unlabeled += 1
        if len(unlabeled_names) < 25:
            try:
                unlabeled_names.append(str(path.relative_to(root)))
            except ValueError:
                unlabeled_names.append(path.name)

    complete = unlabeled == 0
    return {
        "root": str(root),
        "unlabeled": unlabeled,
        "labeled": labeled,
        "skipped": skipped,
        "total_mp4": unlabeled + labeled + skipped,
        "complete": complete,
        "unlabeled_names": unlabeled_names,
        "message": (
            "All footage is inside task folders"
            if complete
            else f"{unlabeled} file(s) still outside task folders"
        ),
    }


def scan_mp4_files(
    root: Path,
    *,
    recursive: bool = True,
    mode: str = "all",
) -> list[dict]:
    """mode: all | raw | clips | label"""
    from .ffmpeg_tools import FFmpegNotFoundError, ffmpeg_available

    tools = ffmpeg_available()
    if not tools["ok"]:
        raise FFmpegNotFoundError(tools["hint"] or "FFmpeg / ffprobe not found on PATH")

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    candidates: list[Path] = []
    for path in _iter_mp4_files(root, recursive=recursive):
        if mode == "raw" and is_raw_footage(path, root=root):
            candidates.append(path)
        elif mode == "clips" and is_trimmed_clip(path):
            candidates.append(path)
        elif mode == "label" and is_labelable_footage(path, root=root):
            candidates.append(path)
        elif (
            mode == "all"
            and path.suffix.upper() == ".MP4"
            and not is_hidden_or_temp_mp4(path)
        ):
            candidates.append(path)

    if mode == "label" and candidates:
        # Only finalized clips / wholes — hide sources still being trimmed and
        # any output path that is still a running/queued job target.
        try:
            from .eager_trim_queue import eager_trim_queue

            busy_sources, busy_outputs = eager_trim_queue.active_path_sets()
        except Exception:  # noqa: BLE001
            busy_sources, busy_outputs = set(), set()

        filtered: list[Path] = []
        for path in candidates:
            key = str(path.resolve())
            if key in busy_outputs:
                continue
            if key in busy_sources:
                continue
            # Prefer finalized trimmed clips; also allow whole files kept after clean.
            if is_trimmed_clip(path) or is_raw_footage(path, root=root):
                filtered.append(path)
        candidates = filtered

    ordered = sorted(candidates, key=lambda p: p.name.lower())
    if not ordered:
        return []
    # Probe in parallel — serial ffprobe over a big folder on a USB drive is slow.
    with ThreadPoolExecutor(max_workers=min(4, len(ordered))) as pool:
        videos = list(pool.map(lambda path: _video_dict(path, root), ordered))
    return videos


def _next_clip_number(source: Path) -> int:
    parent = source.parent
    base = clip_base_stem(source)
    existing = 0
    for path in parent.iterdir():
        if not path.is_file() or path.suffix.upper() != ".MP4":
            continue
        if path.stem.startswith(f"{base}-") and path.stem[len(base) + 1 :].isdigit():
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


def _finish_source_after_trims(source: Path, *, delete_source: bool = True) -> dict:
    """Remove the raw file once all trims for this source are done."""
    from .eager_trim_queue import eager_trim_queue

    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source}")

    failed = [j for j in eager_trim_queue.jobs_for_source(source) if j.status == "failed"]
    if failed:
        raise RuntimeError(
            f"{len(failed)} trim job(s) failed — fix errors before removing the raw file"
        )

    deleted = False
    if delete_source and is_raw_footage(source):
        move_to_trash(source)
        deleted = True

    return {"deleted_source": deleted, "source": str(source)}


def finish_cleaning_file(source: Path, *, delete_source: bool = True) -> dict:
    """Block until trims finish, then remove the raw file."""
    from .eager_trim_queue import eager_trim_queue

    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"Source not found: {source}")

    eager_trim_queue.wait_for_source(source)
    return _finish_source_after_trims(source, delete_source=delete_source)


def assign_clip_to_task(clip_path: Path, label_root: Path, task_name: str) -> dict:
    """Move footage into <task>/ directly under the footage folder."""
    clip_path = clip_path.expanduser().resolve()
    label_root = label_root.expanduser().resolve()
    if not clip_path.exists():
        raise FileNotFoundError(f"Clip not found: {clip_path}")
    if not is_labelable_footage(clip_path, root=label_root):
        raise ValueError("This file cannot be labeled (already in a task folder or not an MP4)")

    task_dir = task_directory(label_root, task_name)
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

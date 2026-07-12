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
GOPRO_MEDIA_DIR_RE = re.compile(r"^\d{3}GOPRO$", re.IGNORECASE)
SKIP_DIR_NAMES = {LABELED_FOLDER.lower(), "labeled", "tasks", ".trash"}
NON_TASK_DIR_NAMES = SKIP_DIR_NAMES | {
    "dcim",
    "misc",
    "private",
    "system volume information",
    "gopro",
    ".trash",
}


def task_directory(label_root: Path, task_name: str) -> Path:
    """Task folder directly beside the footage (no Labeled/ wrapper)."""
    return label_root.expanduser().resolve() / task_folder_name(task_name)


def _looks_like_task_dir_name(name: str) -> bool:
    """True for folders that hold labeled clips (not DCIM / ###GOPRO / camera ids)."""
    if not name or name.startswith("."):
        return False
    lower = name.lower()
    if lower in NON_TASK_DIR_NAMES:
        return False
    if CAMERA_FOLDER_RE.match(name) or GOPRO_MEDIA_DIR_RE.match(name):
        return False
    return True


def is_under_task_folder(path: Path, root: Path) -> bool:
    root = root.expanduser().resolve()
    try:
        resolved = path.expanduser().resolve()
        rel = resolved.parent.relative_to(root)
    except ValueError:
        return False
    if not rel.parts:
        return False
    task_slugs = {task_folder_name(task) for task in load_tasks()}
    first = rel.parts[0]
    if CAMERA_FOLDER_RE.match(first):
        if len(rel.parts) < 2:
            return False
        second = rel.parts[1]
        return second in task_slugs or _looks_like_task_dir_name(second)
    return first in task_slugs or _looks_like_task_dir_name(first)


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
    """True for Finder junk and in-progress trim temps (*.partial.MP4 / *.MP4.partial)."""
    name = path.name
    if name.startswith("."):
        return True
    lower = name.lower()
    return ".partial" in lower


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
        if path.suffix.upper() != ".MP4" or is_hidden_or_temp_mp4(path):
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


def _clip_number_from_stem(stem: str, base: str) -> int | None:
    prefix = f"{base}-"
    if not stem.startswith(prefix):
        return None
    tail = stem[len(prefix) :]
    if tail.isdigit():
        return int(tail)
    return None


def _collect_used_clip_numbers(folder: Path, base: str, used: set[int]) -> None:
    if not folder.is_dir():
        return
    try:
        entries = list(folder.iterdir())
    except OSError:
        return
    for path in entries:
        if not path.is_file() or path.suffix.upper() != ".MP4":
            continue
        if is_hidden_or_temp_mp4(path):
            continue
        number = _clip_number_from_stem(path.stem, base)
        if number is not None:
            used.add(number)


def _next_clip_number(source: Path, reserved: set[int] | None = None) -> int:
    """Next -N for this stem. Includes sibling task folders so labeled clips keep their numbers."""
    parent = source.parent
    base = clip_base_stem(source)
    used: set[int] = set(reserved or ())

    _collect_used_clip_numbers(parent, base, used)

    # Also scan nearby task folders (beside DCIM / under camera / card root).
    # Stay shallow — never walk into the system temp / drive root junk.
    ancestors: list[Path] = []
    cursor = parent
    for _ in range(3):
        ancestors.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent

    task_slugs = {task_folder_name(t) for t in load_tasks()}
    for ancestor in ancestors:
        try:
            siblings = list(ancestor.iterdir())
        except OSError:
            continue
        for sibling in siblings:
            if not sibling.is_dir():
                continue
            try:
                if sibling.resolve() == parent.resolve():
                    continue
            except OSError:
                continue
            name = sibling.name
            if not (_looks_like_task_dir_name(name) or name in task_slugs):
                continue
            _collect_used_clip_numbers(sibling, base, used)

    next_number = 1
    while next_number in used:
        next_number += 1
    return next_number


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


def _unique_label_dest(task_dir: Path, filename: str) -> Path:
    dest = task_dir / filename
    if not dest.exists():
        return dest
    stem = Path(filename).stem
    suffix = Path(filename).suffix or ".MP4"
    for index in range(2, 1000):
        candidate = task_dir / f"{stem}__{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Already exists: {dest}")


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
        try:
            same_size = dest.stat().st_size == clip_path.stat().st_size
        except OSError:
            same_size = False
        if same_size:
            # Prior label left a duplicate in DCIM (or cross-device copy remnant).
            try:
                clip_path.unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"Already in {task_name}, but could not remove leftover source: {exc}"
                ) from exc
            return {
                "task": task_name,
                "task_dir": str(task_dir),
                "output": str(dest),
                "moved": False,
                "already_there": True,
            }
        dest = _unique_label_dest(task_dir, clip_path.name)

    shutil.move(str(clip_path), str(dest))
    if clip_path.exists():
        # Cross-device move sometimes leaves the source; force-remove if dest is good.
        try:
            if dest.is_file() and dest.stat().st_size > 0:
                clip_path.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Moved to {task_name}, but leftover source remains: {exc}"
            ) from exc

    return {
        "task": task_name,
        "task_dir": str(task_dir),
        "output": str(dest),
        "moved": True,
        "already_there": False,
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

"""Trim GoPro footage while preserving the GPMF / IMU metadata track."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .probe import MediaInfo, probe_media
from .timestamps import format_timestamp

MIN_FREE_BYTES = 500 * 1024 * 1024


_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
_COPY_SUFFIX_RE = re.compile(r"\s+copy(?:\s+\d+)?$", re.IGNORECASE)


def clip_base_stem(path: Path | str) -> str:
    """Base name for trimmed clips, e.g. 'GH012332 copy' -> 'GH012332'."""
    stem = path.stem if isinstance(path, Path) else Path(str(path)).stem
    cleaned = _COPY_SUFFIX_RE.sub("", stem).strip()
    return cleaned or stem


@dataclass
class TrimJob:
    job_id: str
    input_path: Path
    output_path: Path
    start_seconds: float
    end_seconds: float
    clip_number: int = 1
    batch_id: str | None = None
    status: str = "queued"
    progress: float = 0.0
    message: str = ""
    error: str | None = None
    command: str = ""


@dataclass
class JobStore:
    jobs: dict[str, TrimJob] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def create(self, job: TrimJob) -> None:
        with self.lock:
            self.jobs[job.job_id] = job

    def get(self, job_id: str) -> TrimJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def update(self, job_id: str, **kwargs) -> None:
        with self.lock:
            job = self.jobs[job_id]
            for key, value in kwargs.items():
                setattr(job, key, value)


job_store = JobStore()


def find_udtacopy() -> Path | None:
    candidates = [
        Path(__file__).resolve().parents[1] / "bin" / "udtacopy",
        Path(__file__).resolve().parents[2] / "bin" / "udtacopy",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return shutil.which("udtacopy")


def build_output_path(
    input_path: Path,
    clip_number: int,
    output_dir: Path | None = None,
) -> Path:
    directory = output_dir or input_path.parent
    stem = clip_base_stem(input_path)
    suffix = input_path.suffix or ".MP4"
    return directory / f"{stem}-{clip_number}{suffix}"


def build_ffmpeg_command(
    media: MediaInfo,
    output_path: Path,
    start_seconds: float,
    duration_seconds: float,
) -> list[str]:
    if media.video_index is None:
        raise RuntimeError("No video stream found in file")
    if duration_seconds <= 0:
        raise RuntimeError("Clip duration must be greater than zero")

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-progress",
        "pipe:2",
        "-nostats",
        "-y",
        "-ss",
        format_timestamp(start_seconds),
        "-i",
        str(media.path),
        "-t",
        format_timestamp(duration_seconds),
        "-map",
        f"0:{media.video_index}",
    ]

    if media.audio_index is not None:
        command.extend(["-map", f"0:{media.audio_index}"])

    if media.gpmf_index is not None:
        data_tag_index = 2 if media.audio_index is not None else 1
        command.extend(
            [
                "-map",
                f"0:{media.gpmf_index}",
                "-copy_unknown",
                f"-tag:d:{data_tag_index}",
                "gpmd",
            ]
        )

    command.extend(["-avoid_negative_ts", "make_zero", "-c", "copy", str(output_path)])
    return command


def _parse_progress(line: str, duration_seconds: float) -> float | None:
    match = _TIME_RE.search(line)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    current = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration_seconds <= 0:
        return 0.0
    return min(99.0, (current / duration_seconds) * 100.0)


def _run_udtacopy(source: Path, target: Path) -> None:
    tool = find_udtacopy()
    if tool is None:
        return
    subprocess.run([str(tool), str(source), str(target)], check=True)


def _run_ffmpeg(command: list[str], duration_seconds: float, job_id: str) -> None:
    process = subprocess.Popen(
        command,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stderr is not None
    stderr_lines: list[str] = []
    for line in process.stderr:
        stripped = line.strip()
        if stripped:
            stderr_lines.append(stripped)
        progress = _parse_progress(line, duration_seconds)
        if progress is not None:
            job_store.update(job_id, progress=progress)

    return_code = process.wait()
    if return_code != 0:
        details = "\n".join(stderr_lines[-8:]) or "No ffmpeg output captured"
        raise RuntimeError(f"ffmpeg failed while trimming the clip:\n{details}")


def _execute_trim(job: TrimJob) -> None:
    temp_path: Path | None = None
    try:
        free_bytes = shutil.disk_usage(job.output_path.parent).free
        if free_bytes < MIN_FREE_BYTES:
            raise RuntimeError(
                f"Not enough free space on drive ({free_bytes // (1024 * 1024)} MB left). "
                "Free space before trimming more footage."
            )

        media = probe_media(job.input_path)
        duration_seconds = job.end_seconds - job.start_seconds
        if media.duration is not None and job.end_seconds > media.duration + 0.5:
            raise RuntimeError(
                f"End time {format_timestamp(job.end_seconds)} exceeds "
                f"video duration {format_timestamp(media.duration)}"
            )

        output_dir = job.output_path.parent
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fallback_dir = Path.home() / "Movies" / "GoPro Cleaned Clips"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            job.output_path = fallback_dir / job.output_path.name
            output_dir = job.output_path.parent
            job_store.update(
                job.job_id,
                message=(
                    f"Could not create output folder on drive ({exc}). "
                    f"Saving to {fallback_dir}"
                ),
            )

        temp_fd, temp_name = tempfile.mkstemp(
            suffix=job.output_path.suffix or ".MP4",
            prefix=f".{job.output_path.stem}_",
            dir=output_dir,
        )
        os.close(temp_fd)
        temp_path = Path(temp_name)

        command = build_ffmpeg_command(
            media,
            temp_path,
            job.start_seconds,
            duration_seconds,
        )
        job_store.update(
            job.job_id,
            status="running",
            message="Trimming clip with ffmpeg...",
            command=" ".join(command),
        )

        try:
            _run_ffmpeg(command, duration_seconds, job.job_id)
        except RuntimeError:
            fallback_dir = Path.home() / "Movies" / "GoPro Cleaned Clips"
            if job.output_path.parent != fallback_dir:
                fallback_dir.mkdir(parents=True, exist_ok=True)
                fallback_output = fallback_dir / job.output_path.name
                if fallback_output.exists():
                    fallback_output.unlink()
                fallback_temp = fallback_dir / temp_path.name
                fallback_command = build_ffmpeg_command(
                    media,
                    fallback_temp,
                    job.start_seconds,
                    duration_seconds,
                )
                job_store.update(
                    job.job_id,
                    message=(
                        "Drive write failed, retrying in ~/Movies/GoPro Cleaned Clips ..."
                    ),
                    command=" ".join(fallback_command),
                )
                if temp_path.exists():
                    temp_path.unlink()
                _run_ffmpeg(fallback_command, duration_seconds, job.job_id)
                temp_path = fallback_temp
                job.output_path = fallback_output
            else:
                raise

        if temp_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced an empty output file")

        if job.output_path.exists():
            job.output_path.unlink()
        temp_path.replace(job.output_path)
        temp_path = None

        if media.has_gpmf:
            job_store.update(
                job.job_id,
                progress=99.0,
                message="Restoring GoPro metadata headers...",
            )
            try:
                _run_udtacopy(job.input_path, job.output_path)
            except subprocess.CalledProcessError as exc:
                job_store.update(
                    job.job_id,
                    message=(
                        "Clip created, but optional udtacopy metadata restore failed. "
                        "IMU track should still be present."
                    ),
                )

        trimmed = probe_media(job.output_path)
        if media.has_gpmf and not trimmed.has_gpmf:
            raise RuntimeError(
                "Trim completed but the output file is missing the GoPro IMU/GPMF track."
            )

        imu_note = ""
        if media.has_gpmf:
            if trimmed.duration is not None:
                drift = abs(trimmed.duration - duration_seconds)
                if drift > 5.0:
                    imu_note = (
                        f" (duration drift {drift:.1f}s — check sync in your IMU tool)"
                    )
            imu_note = f" — IMU/GPMF preserved{imu_note}"

        job_store.update(
            job.job_id,
            status="completed",
            progress=100.0,
            message=f"Saved {job.output_path}{imu_note}",
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to UI
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        job_store.update(
            job.job_id,
            status="failed",
            error=str(exc),
            message="Trim failed",
        )


def move_to_trash(path: Path) -> None:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(path)

    try:
        from send2trash import send2trash

        send2trash(str(path))
        return
    except ImportError:
        pass

    trash_dir = Path.home() / ".Trash"
    destination = trash_dir / path.name
    counter = 1
    while destination.exists():
        destination = trash_dir / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(destination))

"""Background trim queue for Eager Review — trim while continuing to mark clips."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .eager import _finish_source_after_trims, _next_clip_number
from .probe import probe_media
from .trimmer import TrimJob, _execute_trim, build_output_path, clip_base_stem, job_store


@dataclass
class EagerTrimRecord:
    job_id: str
    source_path: str
    start_seconds: float
    end_seconds: float
    status: str = "queued"
    output: str | None = None
    error: str | None = None
    trim_job_id: str | None = None
    clip_number: int | None = None
    source_has_gpmf: bool | None = None
    output_has_gpmf: bool | None = None
    created_at: float = field(default_factory=time.time)


class EagerTrimQueue:
    def __init__(self) -> None:
        self._pending: deque[str] = deque()
        self._records: dict[str, EagerTrimRecord] = {}
        self._pending_finish: set[str] = set()
        self._finish_errors: dict[str, str] = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="eager-trim-queue")
        self._worker.start()

    def submit(self, source: Path, start_seconds: float, end_seconds: float) -> EagerTrimRecord:
        source = source.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"Source not found: {source}")
        if end_seconds <= start_seconds:
            raise ValueError("Clip end must be after start")

        try:
            source_has_gpmf = probe_media(source).has_gpmf
        except (RuntimeError, OSError):
            source_has_gpmf = None

        job_id = str(uuid.uuid4())
        with self._condition:
            reserved = self._reserved_clip_numbers_locked(source)
            clip_number = _next_clip_number(source, reserved=reserved)
            output_path = build_output_path(source, clip_number, source.parent)
            # Avoid colliding with a file already on disk while another job is mid-flight.
            while output_path.exists() or clip_number in reserved:
                reserved.add(clip_number)
                clip_number = _next_clip_number(source, reserved=reserved)
                output_path = build_output_path(source, clip_number, source.parent)

            record = EagerTrimRecord(
                job_id=job_id,
                source_path=str(source),
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                source_has_gpmf=source_has_gpmf,
                clip_number=clip_number,
                output=str(output_path),
            )
            self._records[job_id] = record
            self._pending.append(job_id)
            self._condition.notify()
        return record

    def _reserved_clip_numbers_locked(self, source: Path) -> set[int]:
        """Clip numbers already claimed by queued/running/completed jobs for this stem."""
        base = clip_base_stem(source)
        parent = str(source.parent)
        reserved: set[int] = set()
        for record in self._records.values():
            if record.status == "failed":
                continue
            other = Path(record.source_path)
            if str(other.parent) != parent:
                continue
            if clip_base_stem(other) != base:
                continue
            if record.clip_number is not None:
                reserved.add(int(record.clip_number))
            elif record.output:
                stem = Path(record.output).stem
                prefix = f"{base}-"
                if stem.startswith(prefix) and stem[len(prefix) :].isdigit():
                    reserved.add(int(stem[len(prefix) :]))
        return reserved

    def get(self, job_id: str) -> EagerTrimRecord | None:
        with self._lock:
            return self._records.get(job_id)

    def jobs_for_source(self, source: Path) -> list[EagerTrimRecord]:
        key = str(source.expanduser().resolve())
        with self._lock:
            items = [r for r in self._records.values() if r.source_path == key]
        items.sort(key=lambda r: r.created_at)
        return items

    def active_count_for_source(self, source: Path) -> int:
        key = str(source.expanduser().resolve())
        with self._lock:
            return sum(
                1
                for r in self._records.values()
                if r.source_path == key and r.status in {"queued", "running"}
            )

    def any_active(self) -> bool:
        with self._lock:
            return any(r.status in {"queued", "running"} for r in self._records.values())

    def active_path_sets(self) -> tuple[set[str], set[str]]:
        """Return (busy_source_paths, busy_output_paths) for in-flight trims."""
        sources: set[str] = set()
        outputs: set[str] = set()
        with self._lock:
            for record in self._records.values():
                if record.status not in {"queued", "running"}:
                    continue
                sources.add(record.source_path)
                if record.output:
                    outputs.add(record.output)
                elif record.trim_job_id:
                    trim_job = job_store.get(record.trim_job_id)
                    if trim_job and getattr(trim_job, "output_path", None):
                        outputs.add(str(trim_job.output_path))
        return sources, outputs

    def status_all(self) -> dict:
        """Lightweight global trim progress for clean + label UIs."""
        with self._lock:
            records = list(self._records.values())
        jobs_out: list[dict] = []
        eta_total = 0.0
        active = 0
        for record in sorted(records, key=lambda r: r.created_at, reverse=True):
            if record.status not in {"queued", "running", "completed", "failed"}:
                continue
            if record.status in {"queued", "running"}:
                active += 1
            duration = max(0.0, record.end_seconds - record.start_seconds)
            progress = 0.0
            remaining = 0.0
            message = ""
            if record.status == "queued":
                remaining = duration
                eta_total += duration
            elif record.status == "running":
                if record.trim_job_id:
                    trim_job = job_store.get(record.trim_job_id)
                    if trim_job:
                        progress = float(trim_job.progress or 0)
                        message = trim_job.message or ""
                remaining = duration * max(0.0, 1.0 - progress / 100.0)
                eta_total += remaining
            source_name = Path(record.source_path).name
            jobs_out.append(
                {
                    "job_id": record.job_id,
                    "source_path": record.source_path,
                    "source_name": source_name,
                    "status": record.status,
                    "start_seconds": record.start_seconds,
                    "end_seconds": record.end_seconds,
                    "duration_seconds": duration,
                    "progress": round(progress, 1),
                    "remaining_seconds": round(remaining, 1),
                    "message": message,
                    "output": record.output,
                    "error": record.error,
                }
            )
            if len(jobs_out) >= 40:
                break
        return {
            "active": active,
            "eta_total_seconds": round(eta_total, 1),
            "jobs": jobs_out,
        }

    def schedule_source_finish(self, source: Path) -> dict:
        """Delete raw file after queued trims finish; returns immediately if trims still running."""
        source = source.expanduser().resolve()
        key = str(source)
        with self._condition:
            self._pending_finish.add(key)
        return self._try_finish_source(source)

    def status_for_source(self, source: Path) -> dict:
        jobs = self.jobs_for_source(source)
        jobs_out: list[dict] = []
        eta_total = 0.0
        active = 0

        for record in jobs:
            duration = record.end_seconds - record.start_seconds
            progress = 0.0
            remaining = 0.0
            message = ""

            if record.status in {"queued", "running"}:
                active += 1

            if record.status == "queued":
                remaining = duration
                eta_total += duration
            elif record.status == "running":
                if record.trim_job_id:
                    trim_job = job_store.get(record.trim_job_id)
                    if trim_job:
                        progress = trim_job.progress
                        message = trim_job.message
                remaining = duration * max(0.0, 1.0 - progress / 100.0)
                eta_total += remaining

            jobs_out.append(
                {
                    "job_id": record.job_id,
                    "status": record.status,
                    "start_seconds": record.start_seconds,
                    "end_seconds": record.end_seconds,
                    "duration_seconds": duration,
                    "progress": round(progress, 1),
                    "remaining_seconds": round(remaining, 1),
                    "message": message,
                    "output": record.output,
                    "error": record.error,
                    "source_has_gpmf": record.source_has_gpmf,
                    "output_has_gpmf": record.output_has_gpmf,
                }
            )

        key = str(source.expanduser().resolve())
        with self._lock:
            finish_pending = key in self._pending_finish
            finish_error = self._finish_errors.get(key)

        return {
            "jobs": jobs_out,
            "active": active,
            "eta_total_seconds": round(eta_total, 1),
            "finish_pending": finish_pending,
            "finish_error": finish_error,
        }

    def wait_for_source(self, source: Path, *, timeout: float = 3600) -> bool:
        key = str(source.expanduser().resolve())
        deadline = time.time() + timeout
        with self._condition:
            while True:
                pending = any(
                    r.source_path == key and r.status in {"queued", "running"}
                    for r in self._records.values()
                )
                if not pending:
                    return True
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=min(remaining, 0.5))

    def _try_finish_source(self, source: Path) -> dict:
        source = source.expanduser().resolve()
        key = str(source)
        with self._condition:
            if key not in self._pending_finish:
                return {"scheduled": False, "deleted_source": False, "active": 0}
            active = sum(
                1
                for r in self._records.values()
                if r.source_path == key and r.status in {"queued", "running"}
            )
        if active > 0:
            return {"scheduled": True, "deleted_source": False, "active": active}
        try:
            result = _finish_source_after_trims(source, delete_source=True)
        except Exception as exc:
            with self._condition:
                self._finish_errors[key] = str(exc)
            raise
        with self._condition:
            self._pending_finish.discard(key)
            self._finish_errors.pop(key, None)
        return {"scheduled": False, "deleted_source": result.get("deleted_source", False), "active": 0}

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._pending:
                    self._condition.wait()
                job_id = self._pending.popleft()
                record = self._records.get(job_id)
                if record is None:
                    continue
                record.status = "running"

            if record is None:
                continue

            source = Path(record.source_path)
            try:
                clip_number = record.clip_number
                if clip_number is None:
                    with self._lock:
                        reserved = self._reserved_clip_numbers_locked(source)
                    clip_number = _next_clip_number(source, reserved=reserved)
                    record.clip_number = clip_number
                output_path = Path(record.output) if record.output else build_output_path(
                    source, clip_number, source.parent
                )
                if output_path.exists():
                    # Rare race with a leftover file — pick a free number instead of failing.
                    with self._lock:
                        reserved = self._reserved_clip_numbers_locked(source)
                        reserved.add(clip_number)
                    clip_number = _next_clip_number(source, reserved=reserved)
                    output_path = build_output_path(source, clip_number, source.parent)
                    record.clip_number = clip_number
                    record.output = str(output_path)

                trim_job = TrimJob(
                    job_id=str(uuid.uuid4()),
                    input_path=source,
                    output_path=output_path,
                    start_seconds=record.start_seconds,
                    end_seconds=record.end_seconds,
                    clip_number=clip_number,
                )
                record.trim_job_id = trim_job.job_id
                record.output = str(output_path)
                job_store.create(trim_job)
                _execute_trim(trim_job)
                finished = job_store.get(trim_job.job_id)
                if finished is None or finished.status != "completed":
                    raise RuntimeError(finished.error if finished else "Trim failed")

                try:
                    record.output_has_gpmf = probe_media(output_path).has_gpmf
                except (RuntimeError, OSError):
                    record.output_has_gpmf = False

                if record.source_has_gpmf and not record.output_has_gpmf:
                    raise RuntimeError(
                        "Trim completed but the output file is missing the GoPro IMU/GPMF track."
                    )

                with self._condition:
                    record.status = "completed"
                    record.output = str(output_path)
                    self._condition.notify_all()
            except Exception as exc:  # noqa: BLE001
                with self._condition:
                    record.status = "failed"
                    record.error = str(exc)
                    self._condition.notify_all()
            else:
                try:
                    self._try_finish_source(source)
                except Exception as exc:
                    with self._condition:
                        self._finish_errors[str(source)] = str(exc)


eager_trim_queue = EagerTrimQueue()

"""Background trim queue for Eager Review — trim while continuing to mark clips."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .eager import _finish_source_after_trims, _next_clip_number
from .probe import probe_media
from .trimmer import TrimJob, _execute_trim, build_output_path, clip_base_stem, job_store


class _JobCancelled(Exception):
    """Internal signal that a queued/running record was cancelled."""


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
    kind: str = "trim"
    task: str | None = None
    source_has_gpmf: bool | None = None
    output_has_gpmf: bool | None = None
    created_at: float = field(default_factory=time.time)


def _worker_count() -> int:
    """Stream-copy trims are I/O bound — a small pool overlaps them safely."""
    try:
        n = int(os.environ.get("GOPRO_TRIM_WORKERS", "2"))
    except ValueError:
        n = 2
    return max(1, min(4, n))


class EagerTrimQueue:
    def __init__(self) -> None:
        self._pending: deque[str] = deque()
        self._records: dict[str, EagerTrimRecord] = {}
        # path -> whether to delete the raw file once its trims finish
        self._pending_finish: dict[str, bool] = {}
        self._finish_errors: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._workers = [
            threading.Thread(target=self._worker_loop, daemon=True, name=f"eager-trim-queue-{i}")
            for i in range(_worker_count())
        ]
        for worker in self._workers:
            worker.start()

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns True if it was still active.

        Queued jobs are dropped immediately; running jobs have their ffmpeg
        process terminated so the encode stops mid-flight.
        """
        with self._condition:
            record = self._records.get(job_id)
            if record is None or record.status not in {"queued", "running"}:
                return False
            self._cancelled.add(job_id)
            if record.status == "queued":
                record.status = "cancelled"
                record.error = "Cancelled"
                self._cancelled.discard(job_id)
                try:
                    self._pending.remove(job_id)
                except ValueError:
                    pass
                self._condition.notify_all()
                return True
            trim_job_id = record.trim_job_id

        # Running — terminate the ffmpeg encode outside the lock.
        if trim_job_id:
            trim_job = job_store.get(trim_job_id)
            if trim_job is not None:
                job_store.update(trim_job_id, cancel_requested=True)
                proc = getattr(trim_job, "process", None)
                if proc is not None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
        return True

    def cancel_all(self) -> int:
        """Cancel every queued and running job. Returns how many were cancelled."""
        with self._lock:
            active_ids = [
                r.job_id for r in self._records.values() if r.status in {"queued", "running"}
            ]
        return sum(1 for jid in active_ids if self.cancel_job(jid))

    def has_equivalent_job(self, source: Path, start_seconds: float, end_seconds: float) -> bool:
        """True when a non-failed job already covers this exact source segment."""
        key = str(source.expanduser().resolve())
        with self._lock:
            return any(
                r.source_path == key
                and r.status in {"queued", "running", "completed"}
                and abs(r.start_seconds - start_seconds) < 0.05
                and abs(r.end_seconds - end_seconds) < 0.05
                for r in self._records.values()
            )

    def submit(
        self,
        source: Path,
        start_seconds: float,
        end_seconds: float,
        *,
        output_dir: Path | None = None,
        kind: str = "trim",
        task: str | None = None,
    ) -> EagerTrimRecord:
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
            target_dir = output_dir.expanduser().resolve() if output_dir else source.parent
            target_dir.mkdir(parents=True, exist_ok=True)
            output_path = build_output_path(source, clip_number, target_dir)
            # Avoid colliding with a file already on disk while another job is mid-flight.
            while output_path.exists() or clip_number in reserved:
                reserved.add(clip_number)
                clip_number = _next_clip_number(source, reserved=reserved)
                output_path = build_output_path(source, clip_number, target_dir)

            record = EagerTrimRecord(
                job_id=job_id,
                source_path=str(source),
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                source_has_gpmf=source_has_gpmf,
                clip_number=clip_number,
                output=str(output_path),
                kind=kind,
                task=task,
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
        active_records = [r for r in records if r.status in {"queued", "running"}]
        other_records = [r for r in records if r.status in {"completed", "failed", "cancelled"}]
        jobs_out: list[dict] = []
        eta_total = 0.0
        active = len(active_records)

        def _job_payload(record) -> dict:
            duration = max(0.0, record.end_seconds - record.start_seconds)
            progress = 0.0
            remaining = 0.0
            message = ""
            if record.status == "queued":
                remaining = duration
            elif record.status == "running":
                if record.trim_job_id:
                    trim_job = job_store.get(record.trim_job_id)
                    if trim_job:
                        progress = float(trim_job.progress or 0)
                        message = trim_job.message or ""
                remaining = duration * max(0.0, 1.0 - progress / 100.0)
            return {
                "job_id": record.job_id,
                "source_path": record.source_path,
                "source_name": Path(record.source_path).name,
                "status": record.status,
                "start_seconds": record.start_seconds,
                "end_seconds": record.end_seconds,
                "duration_seconds": duration,
                "progress": round(progress, 1),
                "remaining_seconds": round(remaining, 1),
                "message": message,
                "output": record.output,
                "error": record.error,
                "kind": record.kind,
                "task": record.task,
            }

        # Always include every active job first (never drop them behind a 40-cap).
        for record in sorted(active_records, key=lambda r: r.created_at):
            payload = _job_payload(record)
            eta_total += float(payload["remaining_seconds"] or 0)
            jobs_out.append(payload)

        for record in sorted(other_records, key=lambda r: r.created_at, reverse=True):
            if len(jobs_out) >= 40:
                break
            jobs_out.append(_job_payload(record))

        return {
            "active": active,
            "eta_total_seconds": round(eta_total, 1),
            "jobs": jobs_out,
        }

    def schedule_source_finish(self, source: Path, *, delete_source: bool = False) -> dict:
        """Optionally delete the raw file after its trims finish.

        Returns immediately if trims are still running; the worker retries when
        the last job for this source completes. ``delete_source`` defaults to
        False — once True is recorded for a path it sticks for that finish pass.
        """
        source = source.expanduser().resolve()
        key = str(source)
        with self._condition:
            prev = self._pending_finish.get(key, False)
            self._pending_finish[key] = bool(prev or delete_source)
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
                    "kind": record.kind,
                    "task": record.task,
                    "source_has_gpmf": record.source_has_gpmf,
                    "output_has_gpmf": record.output_has_gpmf,
                }
            )

        key = str(source.expanduser().resolve())
        with self._lock:
            finish_pending = key in self._pending_finish
            finish_delete = bool(self._pending_finish.get(key))
            finish_error = self._finish_errors.get(key)

        return {
            "jobs": jobs_out,
            "active": active,
            "eta_total_seconds": round(eta_total, 1),
            "finish_pending": finish_pending,
            "finish_delete_source": finish_delete,
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
            delete_source = bool(self._pending_finish.get(key))
            active = sum(
                1
                for r in self._records.values()
                if r.source_path == key and r.status in {"queued", "running"}
            )
        if active > 0:
            return {"scheduled": True, "deleted_source": False, "active": active}
        try:
            result = _finish_source_after_trims(source, delete_source=delete_source)
        except Exception as exc:
            with self._condition:
                self._finish_errors[key] = str(exc)
            raise
        with self._condition:
            self._pending_finish.pop(key, None)
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
                # Cancelled while still queued — skip without starting ffmpeg.
                if job_id in self._cancelled:
                    self._cancelled.discard(job_id)
                    record.status = "cancelled"
                    record.error = "Cancelled"
                    self._condition.notify_all()
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
                    output_path = build_output_path(source, clip_number, output_path.parent)
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

                # Cancel may have landed between dequeue and job creation.
                with self._condition:
                    if job_id in self._cancelled:
                        job_store.update(trim_job.job_id, cancel_requested=True)

                _execute_trim(trim_job)
                finished = job_store.get(trim_job.job_id)

                with self._condition:
                    was_cancelled = job_id in self._cancelled or (
                        finished is not None and finished.status == "cancelled"
                    )
                    if was_cancelled:
                        self._cancelled.discard(job_id)
                if was_cancelled:
                    raise _JobCancelled()

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
            except _JobCancelled:
                # Remove any partial output the cancelled encode may have left.
                if record.output:
                    try:
                        partial = Path(record.output)
                        if partial.exists():
                            partial.unlink()
                    except OSError:
                        pass
                with self._condition:
                    record.status = "cancelled"
                    record.error = "Cancelled"
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

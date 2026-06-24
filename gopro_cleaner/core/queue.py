"""Background trim queue and batch management."""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .trimmer import TrimJob, _execute_trim, build_output_path, job_store, move_to_trash


MIN_FREE_BYTES = 500 * 1024 * 1024


@dataclass
class BatchRecord:
    batch_id: str
    input_path: Path
    input_name: str
    delete_original: bool
    job_ids: list[str]
    status: str = "queued"
    message: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class TrimQueue:
    pending: deque[str] = field(default_factory=deque)
    batches: dict[str, BatchRecord] = field(default_factory=dict)
    job_to_batch: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    condition: threading.Condition = field(init=False)
    worker: threading.Thread | None = None

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)
        self.worker = threading.Thread(target=self._worker_loop, daemon=True, name="trim-queue")
        self.worker.start()

    def submit_batch(
        self,
        input_path: Path,
        clips: list[tuple[float, float]],
        delete_original: bool,
    ) -> BatchRecord:
        input_path = input_path.expanduser().resolve()
        batch_id = str(uuid.uuid4())
        job_ids: list[str] = []

        for clip_number, (start_seconds, end_seconds) in enumerate(clips, 1):
            output_path = build_output_path(input_path, clip_number)
            if output_path.exists() and output_path.stat().st_size > 0:
                raise FileExistsError(f"Output already exists: {output_path.name}")

            job = TrimJob(
                job_id=str(uuid.uuid4()),
                input_path=input_path,
                output_path=output_path,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                clip_number=clip_number,
                batch_id=batch_id,
            )
            job_ids.append(job.job_id)
            job_store.create(job)

        batch = BatchRecord(
            batch_id=batch_id,
            input_path=input_path,
            input_name=input_path.name,
            delete_original=delete_original,
            job_ids=job_ids,
        )

        with self.condition:
            self.batches[batch_id] = batch
            for job_id in job_ids:
                self.job_to_batch[job_id] = batch_id
                self.pending.append(job_id)
            self.condition.notify()

        return batch

    def get_batch(self, batch_id: str) -> BatchRecord | None:
        with self.lock:
            return self.batches.get(batch_id)

    def list_batches(self, limit: int | None = None) -> list[BatchRecord]:
        with self.lock:
            batches = list(self.batches.values())
        status_rank = {"running": 0, "queued": 1, "failed": 2, "completed": 3}
        batches.sort(
            key=lambda batch: (status_rank.get(batch.status, 9), batch.created_at),
        )
        if limit is not None:
            return batches[:limit]
        return batches

    def batch_counts(self) -> dict[str, int]:
        with self.lock:
            batches = list(self.batches.values())
        counts = {"total": len(batches), "queued": 0, "running": 0, "completed": 0, "failed": 0}
        for batch in batches:
            counts[batch.status] = counts.get(batch.status, 0) + 1
        return counts

    def _worker_loop(self) -> None:
        while True:
            with self.condition:
                while not self.pending:
                    self.condition.wait()
                job_id = self.pending.popleft()

            job = job_store.get(job_id)
            if job is None:
                continue

            batch = self.get_batch(job.batch_id) if job.batch_id else None
            if batch and batch.status == "queued":
                self._update_batch(batch.batch_id, status="running", message="Trimming clips...")

            _execute_trim(job)
            self._handle_job_finished(job_id)

    def _handle_job_finished(self, job_id: str) -> None:
        batch_id = self.job_to_batch.get(job_id)
        if not batch_id:
            return

        batch = self.get_batch(batch_id)
        if batch is None:
            return

        jobs = [job_store.get(item_id) for item_id in batch.job_ids]
        jobs = [job for job in jobs if job is not None]
        if not jobs:
            return

        if any(job.status in {"queued", "running"} for job in jobs):
            completed = sum(job.status == "completed" for job in jobs)
            self._update_batch(
                batch_id,
                status="running",
                message=f"Trimming clip {completed + 1} of {len(jobs)}...",
            )
            return

        failed_jobs = [job for job in jobs if job.status == "failed"]
        if failed_jobs:
            self._update_batch(
                batch_id,
                status="failed",
                message=(
                    f"{len(failed_jobs)} of {len(jobs)} clips failed. "
                    "Original file was not deleted."
                ),
            )
            return

        message = f"Exported {len(jobs)} clips for {batch.input_name}"
        if batch.delete_original:
            if not _clips_verified_on_disk(jobs):
                self._update_batch(
                    batch_id,
                    status="completed",
                    message=f"{message}, but original was kept (clip files missing on disk)",
                )
                return
            try:
                free_bytes = shutil.disk_usage(batch.input_path.parent).free
                if free_bytes < MIN_FREE_BYTES:
                    self._update_batch(
                        batch_id,
                        status="completed",
                        message=(
                            f"{message}, but original was kept "
                            f"(drive low on space: {free_bytes // (1024 * 1024)} MB free)"
                        ),
                    )
                    return
                if batch.input_path.exists():
                    move_to_trash(batch.input_path)
                    message = (
                        f"Exported {len(jobs)} clips and moved "
                        f"{batch.input_name} to Trash"
                    )
            except Exception as exc:  # noqa: BLE001
                self._update_batch(
                    batch_id,
                    status="completed",
                    message=f"{message}, but deleting original failed: {exc}",
                )
                return

        self._update_batch(batch_id, status="completed", message=message)

    def _update_batch(self, batch_id: str, **kwargs) -> None:
        with self.lock:
            batch = self.batches.get(batch_id)
            if batch is None:
                return
            for key, value in kwargs.items():
                setattr(batch, key, value)


def _clips_verified_on_disk(jobs: list[TrimJob]) -> bool:
    for job in jobs:
        if job.status != "completed":
            return False
        if not job.output_path.exists():
            return False
        if job.output_path.stat().st_size == 0:
            return False
    return True


trim_queue = TrimQueue()

"""v2 addition: Slack alerts + capped auto-retry for AWS upload jobs.

Deliberately built as a standalone watcher on top of aws_upload's existing
public API (list_jobs / get_job / restart_job) instead of editing aws_upload.py
itself — the already-running upload/monitor code stays untouched. This module
is purely additive: if it's never started (see ``ensure_started``), or the
Slack webhook is unset, nothing about existing behavior changes.

Each failure gets one dedicated thread (``_retry_cycle``) that owns its own
alert -> wait -> retry -> check-outcome loop end to end, rather than inferring
retry progress by polling status snapshots. A poll-based design missed fast
back-to-back failures (error -> running -> error happening faster than the
poll interval looks identical to "still the same old error"), silently
stopping after one attempt instead of the configured cap — this owns the
outcome directly so it can't miss its own transitions.

Config (config.json, same pattern as s5cmd_numworkers/aws_upload_retries):
    slack_webhook_url        — Slack Incoming Webhook URL; empty disables alerts
    aws_auto_retry_max       — auto re-attempts after a hard failure (default 3)
    aws_auto_retry_delay_seconds — wait between auto-retry attempts (default 300)
"""

from __future__ import annotations

import threading
import time

from . import aws_upload, engine, slack_alert, ssd_full_upload_v2
from .config import load_config

_POLL_SECONDS = 5.0
_TERMINAL_RESET_STATUSES = {"completed", "verified", "cancelled", "deleted_local"}

_lock = threading.Lock()
_started = False
_seen: dict[str, dict] = {}  # job_id -> {"retry_in_flight": bool, "last_status": str}


def ensure_started() -> None:
    """Start the v2 alert/auto-retry watcher once. Safe to call repeatedly."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_watch_loop, daemon=True, name="ops-alerts-v2").start()


def _auto_retry_max() -> int:
    try:
        n = int(load_config().get("aws_auto_retry_max") or 3)
    except (TypeError, ValueError):
        n = 3
    return max(0, min(n, 10))


def _auto_retry_delay_seconds() -> float:
    try:
        n = float(load_config().get("aws_auto_retry_delay_seconds") or 300)
    except (TypeError, ValueError):
        n = 300.0
    return max(5.0, min(n, 3600.0))


def _watch_loop() -> None:
    while True:
        try:
            _scan_once()
        except Exception:  # noqa: BLE001 — a watcher bug must never crash the server
            pass
        time.sleep(_POLL_SECONDS)


def _scan_once() -> None:
    """Detect fresh failures once, then hand each one off to its own retry cycle.

    "error" ownership is claimed here (``retry_in_flight``) so a job already
    being retried is never re-dispatched by a later poll. "mismatch" has no
    retry loop, so a simple last-seen-status check is enough to alert once
    per occurrence without re-alerting every 5s while it waits for a manual
    Retry.
    """
    for job in aws_upload.list_jobs():
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        status = str(job.get("status") or "")
        dispatch_error = False
        dispatch_mismatch = False
        with _lock:
            state = _seen.setdefault(job_id, {"retry_in_flight": False, "last_status": ""})
            already_flagged_mismatch = state["last_status"] == status == "mismatch"
            state["last_status"] = status
            if status in _TERMINAL_RESET_STATUSES:
                state["retry_in_flight"] = False
            elif status == "error" and not state["retry_in_flight"]:
                state["retry_in_flight"] = True  # claim ownership before releasing the lock
                dispatch_error = True
            elif status == "mismatch" and not already_flagged_mismatch:
                dispatch_mismatch = True
        if dispatch_error:
            threading.Thread(
                target=_retry_cycle,
                args=(job_id,),
                daemon=True,
                name=f"ops-alerts-retry-{job_id[-12:]}",
            ).start()
        elif dispatch_mismatch:
            _handle_mismatch(job)


def _retry_cycle(job_id: str) -> None:
    """Own one failure end to end: alert, wait, retry, check the outcome, repeat or give up."""
    max_retries = _auto_retry_max()
    try:
        attempt = 0
        while True:
            job = aws_upload.get_job(job_id) or {}
            batch = job.get("batch")
            dest = job.get("dest")
            tail = "\n".join(str(line) for line in (job.get("log") or [])[-5:])
            attempt += 1

            if attempt > max_retries:
                slack_alert.send_alert(
                    f":x: Offloader — AWS sync for batch `{batch}` → `{dest}` failed "
                    f"{max_retries} time(s) and gave up auto-retrying. "
                    f"*Manual Retry needed* in the offloader UI.\nLast log lines:\n{tail}"
                )
                return

            delay = _auto_retry_delay_seconds()
            slack_alert.send_alert(
                f":warning: Offloader — AWS sync failed for batch `{batch}` → `{dest}` "
                f"(attempt {attempt}/{max_retries}). Auto-retrying in {int(delay)}s.\n"
                f"Last log lines:\n{tail}"
            )
            time.sleep(max(0.0, delay))

            current = aws_upload.get_job(job_id)
            if not current or current.get("status") != "error":
                return  # operator already retried manually, cancelled, or job moved on
            try:
                aws_upload.restart_job(job_id)
            except Exception as exc:  # noqa: BLE001
                slack_alert.send_alert(
                    f":x: Offloader — auto-retry could not start for `{job_id}`: {exc}"
                )
                return

            # Wait for THIS attempt to reach a terminal state before deciding the next step
            # — owning the outcome directly instead of leaving it to the next 5s poll.
            deadline = time.time() + max(30.0, delay * 4)
            while True:
                if time.time() >= deadline:
                    return  # still running after a long wait — let it be, don't force another retry
                time.sleep(2.0)
                current = aws_upload.get_job(job_id)
                if not current:
                    return
                status = current.get("status")
                if status == "error":
                    break  # this attempt failed too — loop around to alert/retry again
                if status in _TERMINAL_RESET_STATUSES:
                    return  # succeeded (or operator intervened) — stop the retry cycle
                # else still running/checking — keep waiting
    finally:
        with _lock:
            state = _seen.get(job_id)
            if state:
                state["retry_in_flight"] = False


def _batch_confirmed_ok(job: dict) -> bool:
    """Cross-check a per-job mismatch against the batch's CURRENT footprint
    across every configured SSD — not this job's own frozen ``sources``
    snapshot. A job created early in a batch's life can look permanently
    mismatched even after the batch is genuinely complete, simply because
    more data landed on a second SSD after that job was created (see the
    SD-/CARD-OK job stuck comparing its own old 4MB against the batch's
    real, current 8MB). False positive, not a real problem — don't alert on it.
    """
    batch = str(job.get("batch") or "").strip()
    if not batch:
        return False
    cfg = load_config()
    ssd1 = str(cfg.get("ssd1") or "")
    ssd2 = str(cfg.get("ssd2") or "")
    s3_uri = str(job.get("s3_uri") or cfg.get("s3_uri") or "")
    if not s3_uri or (not ssd1 and not ssd2):
        return False
    try:
        result = ssd_full_upload_v2.verify_batch_v2(batch=batch, ssd1=ssd1, ssd2=ssd2, s3_uri=s3_uri)
    except Exception:  # noqa: BLE001 — cross-check failing is not proof of a real problem
        return False
    return bool(result.get("ok"))


def _handle_mismatch(job: dict) -> None:
    if _batch_confirmed_ok(job):
        engine.log_message(
            f"v2: suppressed a stale mismatch alert for batch `{job.get('batch')}` "
            f"(job {job.get('id')}) — whole-batch verify across all SSDs confirms it's actually fine",
            kind="ok",
        )
        return
    slack_alert.send_alert(
        f":warning: Offloader — size mismatch after upload for batch `{job.get('batch')}` → "
        f"`{job.get('dest')}`: local {job.get('local_bytes')} vs S3 {job.get('s3_bytes')} "
        f"(Δ {job.get('size_delta')}). Manual Retry recommended."
    )

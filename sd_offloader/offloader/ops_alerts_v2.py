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
_seen_cards: dict[str, str] = {}  # card_id -> last_status, for SD->SSD card-copy error alerts
_CARD_ALERT_STATUSES = {"error", "interrupted"}  # not "cancelled" -- that's a deliberate operator action


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
        try:
            _scan_cards_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(_POLL_SECONDS)


def _scan_cards_once() -> None:
    """Alert on SD->SSD card-copy problems (disk full, no space on either SSD,
    interrupted mid-copy, etc.) — separate from the AWS-job watching above,
    since a card failing during the SD->SSD leg never even reaches an AWS
    job at all. Fires once per fresh transition into error/interrupted, same
    dedup approach as the mismatch alert (not on every 5s poll while it sits
    there waiting for a manual Retry).
    """
    status_payload = engine.get_status()
    batch = str((status_payload.get("session") or {}).get("batch") or "")
    for card in status_payload.get("cards") or []:
        card_id = str(card.get("card_id") or "")
        if not card_id:
            continue
        status = str(card.get("status") or "")
        with _lock:
            last_status = _seen_cards.get(card_id, "")
            already_flagged = last_status == status and status in _CARD_ALERT_STATUSES
            _seen_cards[card_id] = status
        if status in _CARD_ALERT_STATUSES and not already_flagged:
            _handle_card_problem(card, batch=batch)


def _handle_card_problem(card: dict, *, batch: str = "") -> None:
    card_id = card.get("card_id")
    status = card.get("status")
    bytes_done = int(card.get("bytes_done") or 0)
    bytes_total = int(card.get("bytes_total") or 0)
    pct = f"{(bytes_done / bytes_total * 100):.0f}%" if bytes_total else "?"
    severity = "critical" if status == "error" else "warning"
    disk = slack_alert.disk_label_from_path(card.get("dest"))
    slack_alert.send_alert(
        f"Card {status}"
        + slack_alert.format_context(batch=batch, disks=disk, card=str(card_id or "")),
        severity=severity,
        fields=[
            ("Card", str(card_id)),
            ("Mount", str(card.get("mount"))),
            ("Destination", str(card.get("dest"))),
            (
                "Progress when it stopped",
                f"{card.get('files_done', 0)}/{card.get('files_total', 0)} files, "
                f"{pct} ({bytes_done}/{bytes_total} bytes)",
            ),
        ],
        detail=str(card.get("message") or ""),
    )


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
            disks = slack_alert.disks_from_sources(job.get("sources"))
            card_id = job.get("card_id")
            context = slack_alert.format_context(batch=str(batch or ""), disks=disks, card=str(card_id or ""))
            tail = "\n".join(str(line) for line in (job.get("log") or [])[-5:])
            attempt += 1

            if attempt > max_retries:
                slack_alert.send_alert(
                    "AWS sync gave up auto-retrying" + context,
                    severity="critical",
                    fields=[
                        ("Batch", str(batch)),
                        ("Destination", str(dest)),
                        ("Attempts", str(max_retries)),
                        ("Action needed", "Manual Retry in the offloader UI"),
                    ],
                    detail=tail,
                )
                return

            delay = _auto_retry_delay_seconds()
            slack_alert.send_alert(
                f"AWS sync failed — retrying ({attempt}/{max_retries})" + context,
                severity="warning",
                fields=[
                    ("Batch", str(batch)),
                    ("Destination", str(dest)),
                    ("Retrying in", f"{int(delay)}s"),
                ],
                detail=tail,
            )
            time.sleep(max(0.0, delay))

            current = aws_upload.get_job(job_id)
            if not current or current.get("status") != "error":
                return  # operator already retried manually, cancelled, or job moved on
            try:
                aws_upload.restart_job(job_id)
            except Exception as exc:  # noqa: BLE001
                slack_alert.send_alert(
                    "Auto-retry could not start" + context,
                    severity="critical",
                    fields=[("Job", str(job_id)), ("Batch", str(batch)), ("Error", str(exc))],
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
    disks = slack_alert.disks_from_sources(job.get("sources"))
    context = slack_alert.format_context(
        batch=str(job.get("batch") or ""), disks=disks, card=str(job.get("card_id") or "")
    )
    slack_alert.send_alert(
        "Size mismatch after upload" + context,
        severity="warning",
        fields=[
            ("Batch", str(job.get("batch"))),
            ("Destination", str(job.get("dest"))),
            ("Local bytes", str(job.get("local_bytes"))),
            ("S3 bytes", str(job.get("s3_bytes"))),
            ("Delta", str(job.get("size_delta"))),
            ("Action needed", "Manual Retry recommended"),
        ],
    )

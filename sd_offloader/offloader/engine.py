"""Session engine: watch SD cards, copy in parallel, optional AWS enqueue."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import aws_upload, batches, eject, embed_meta, inventory, pairing, progress, readers, space
from .config import STATE_DIR, ensure_dirs, load_config, save_config
from .detect import find_card_volumes, list_volumes
from .transfer import copy_file

_lock = threading.RLock()
_session: dict = {
    "active": False,
    "batch": "",
    "mode": "ssd_only",
    "ssd1": "",
    "ssd2": "",
    "s3_uri": "",
    "started_at": None,
    "disk_batches": {},
    "disk_completed": {},
    "closed_batches": {},
    "frozen_disks": {},
}
_cards: dict[str, dict] = {}  # card_id -> job state
_copy_threads: dict[str, threading.Thread] = {}
_batch_dest_locks: dict[str, threading.Lock] = {}
_cancel_requested: set[str] = set()
_waiting_queue: list[dict] = []  # queued starts when at max parallel
_watcher_started = False
_log: list[dict] = []
_notices: list[dict] = []
_watchdog_started = False
WATCHDOG_SECONDS = 30 * 60
# cid -> (bytes_done, last_increase_at) for live copy threads
_watchdog_copy_snap: dict[str, tuple[int, float]] = {}
SNAPSHOT_FILE = STATE_DIR / "ui_snapshot.json"
_last_snapshot_at = 0.0
ACTIVE_COPY_STATUSES = {
    "queued",
    "waiting",
    "copying",
    "verifying",
    "wiping",
    "ejecting",
    "uploading",
    "cancelling",
}
# Operator must click Retry — do not auto-loop these.
MANUAL_RETRY_STATUSES = {"error", "interrupted", "cancelled"}
# Finished / unplugged — next insert may auto-start (same tracking id OK).
HOTPLUG_CLEAR_STATUSES = {"removed", "completed"}


class CopyCancelled(Exception):
    """Raised when the operator cancels an SD→SSD card job."""


def _log_line(message: str, *, kind: str = "info") -> None:
    with _lock:
        _log.append({"t": time.time(), "kind": kind, "message": message})
        if len(_log) > 300:
            del _log[:-300]
    _save_snapshot(force=False)


def push_notice(message: str, *, kind: str = "ok") -> None:
    with _lock:
        _notices.append(
            {
                "id": f"{time.time():.6f}-{len(_notices)}",
                "message": message,
                "kind": kind,
            }
        )
        if len(_notices) > 40:
            del _notices[:-40]


def _save_snapshot(*, force: bool = False) -> None:
    """Persist session/cards/log so reopening the UI still shows live transfers."""
    global _last_snapshot_at
    now = time.time()
    if not force and now - _last_snapshot_at < 1.0:
        return
    _last_snapshot_at = now
    ensure_dirs()
    with _lock:
        payload = {
            "session": dict(_session),
            "cards": [dict(c) for c in _cards.values()],
            "log": list(_log[-120:]),
            "saved_at": now,
        }
    try:
        SNAPSHOT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass


def restore_ui_state() -> None:
    """Load last SD→SSD snapshot and AWS jobs after server start / browser reopen."""
    aws_upload.set_batch_deleted_hook(on_batch_deleted)
    aws_upload.restore_jobs_from_disk()
    try:
        if SNAPSHOT_FILE.exists():
        try:
            data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
                data = None
            if isinstance(data, dict):
        with _lock:
            session = data.get("session")
            if isinstance(session, dict):
                _session.update(session)
                # Watcher must be re-armed after process restart.
                if _session.get("active"):
                    _session["active"] = True
            cards = data.get("cards")
            if isinstance(cards, list):
                for row in cards:
                    if not isinstance(row, dict):
                        continue
                    card_id = str(row.get("card_id") or "").upper()
                    if not card_id:
                        continue
                    status = row.get("status") or ""
                    if status in {"copying", "verifying", "wiping", "ejecting", "uploading", "queued", "scanning"}:
                        # In-flight copy threads died with the old process.
                        if status in {"copying", "verifying"}:
                            row = dict(row)
                            row["status"] = "interrupted"
                            row["message"] = (
                                "Server restarted mid-copy — re-insert card or Start session "
                                "to resume (completed files are skipped)"
                            )
                            row["speed_mbps"] = 0.0
                    _cards[card_id] = dict(row)
            lines = data.get("log")
            if isinstance(lines, list):
                _log.clear()
                _log.extend(line for line in lines if isinstance(line, dict))
        cfg = load_config()
        with _lock:
            if not _session.get("disk_completed"):
                _session["disk_completed"] = dict(cfg.get("disk_completed") or {})
            if not _session.get("disk_batches"):
                _session["disk_batches"] = dict(cfg.get("disk_batches") or {})
            if not _session.get("closed_batches"):
                _session["closed_batches"] = dict(cfg.get("closed_batches") or {})
            if not _session.get("frozen_disks"):
                _session["frozen_disks"] = dict(cfg.get("frozen_disks") or {})
        if _session.get("active"):
            # Do NOT auto-resume the SD watcher on startup — scanning drives during
            # boot was freezing the web UI on hung card readers. User clicks Start.
            _session["active"] = False
            _log_line(
                "Previous session was active — click Start SD → SSD to resume watching",
                kind="ok",
            )
    finally:
        _ensure_watchdog()


def get_status() -> dict:
    with _lock:
        cards = [dict(c) for c in _cards.values()]
        waiting = len(_waiting_queue)
        active = sum(1 for t in _copy_threads.values() if t and t.is_alive())
        status = {
            "session": dict(_session),
            "cards": sorted(cards, key=lambda c: c.get("started_at") or 0, reverse=True),
            "log": list(_log[-80:]),
            "aws_jobs": aws_upload.list_jobs()[:20],
            "parallel": {
                "active": active,
                "waiting": waiting,
                "max": _max_parallel_cards(),
            },
            "capacity": _capacity_estimates(),
            "hotplug_armed": bool(_session.get("active")),
            "notices": list(_notices[-12:]),
            "readers": readers.saved_readers(),
            "disk_batches": dict(_session.get("disk_batches") or {}),
            "disk_completed": dict(_session.get("disk_completed") or {}),
            "closed_batches": dict(_session.get("closed_batches") or {}),
            "frozen_disks": dict(_session.get("frozen_disks") or {}),
        }
    status["disk_batch_states"] = _disk_batch_states()
    _save_snapshot(force=False)
    return status


def _max_parallel_cards() -> int:
    try:
        n = int(load_config().get("max_parallel_cards") or 3)
    except (TypeError, ValueError):
        n = 3
    return max(1, min(n, 8))


def _capacity_estimates() -> dict:
    """Rough day-plan numbers for 40×120 GB cards at common link speeds."""
    cards_per_day = 40
    gb_per_card = 120
    total_gb = cards_per_day * gb_per_card  # 4800
    parallel = _max_parallel_cards()
    # Typical SD→SSD sequential write (USB3 / UHS-I/II varies widely).
    sd_mbs = 100.0
    copy_sec_per_card = (gb_per_card * 1024) / sd_mbs
    copy_waves = (cards_per_day + parallel - 1) // parallel
    copy_hours = (copy_waves * copy_sec_per_card) / 3600.0
    # Network: distinguish gigabit vs gigabyte (operators often say "1 GB/s" meaning 1 Gbps).
    upload_hours_1_gbyte_s = total_gb / 3600.0  # 1 GB/s → 4800s ≈ 1.33h
    upload_hours_1_gbit_s = total_gb / (0.125 * 3600.0)  # 1 Gbps ≈ 125 MB/s → ~10.7h
    return {
        "target_cards_per_day": cards_per_day,
        "gb_per_card": gb_per_card,
        "total_tb": round(total_gb / 1024, 2),
        "parallel_cards": parallel,
        "copy_hours_est": round(copy_hours, 1),
        "upload_hours_at_1_GBps": round(upload_hours_1_gbyte_s, 1),
        "upload_hours_at_1_Gbps": round(upload_hours_1_gbit_s, 1),
        "note": (
            "At a true 1 GB/s (gigabyte) pipe, ~4.8 TB is ~1.3 h of pure upload. "
            "At 1 Gbps (gigabit ~ 125 MB/s), pure upload is ~10.7 h. "
            f"SD->SSD with {parallel} parallel cards at ~100 MB/s is ~{copy_hours:.1f} h of copy wall-time; "
            "SSD+AWS overlaps upload with later cards."
        ),
    }


def start_session(
    *,
    batch: str,
    mode: str,
    ssd1: str,
    ssd2: str,
    s3_uri: str = "",
) -> dict:
    batch = batch.strip()
    if not batch:
        raise ValueError("Batch name is required")
    if mode not in {"ssd_only", "ssd_and_aws"}:
        raise ValueError("mode must be ssd_only or ssd_and_aws")
    if not ssd1 and not ssd2:
        raise ValueError("Pick at least one SSD")
    if mode == "ssd_and_aws" and not s3_uri.strip():
        raise ValueError("S3 URI required for SSD + AWS mode")

    ssd1_path = str(Path(ssd1).resolve()) if ssd1 else ""
    ssd2_path = str(Path(ssd2).resolve()) if ssd2 else ""
    for path in (ssd1_path, ssd2_path):
        if path and not Path(path).exists():
            raise ValueError(f"SSD path not found: {path}")

    cfg = load_config()
    with _lock:
        prev_batches = dict(_session.get("disk_batches") or cfg.get("disk_batches") or {})
        prev_frozen = dict(_session.get("frozen_disks") or cfg.get("frozen_disks") or {})
        prev_completed = dict(_session.get("disk_completed") or cfg.get("disk_completed") or {})
        prev_closed = dict(_session.get("closed_batches") or cfg.get("closed_batches") or {})
        new_keys = {space.path_key(p) for p in (ssd1_path, ssd2_path) if p}
        _session.update(
            {
                "active": True,
                "batch": batch,
                "mode": mode,
                "ssd1": ssd1_path,
                "ssd2": ssd2_path,
                "s3_uri": s3_uri.strip(),
                "started_at": time.time(),
                "disk_batches": {k: v for k, v in prev_batches.items() if k in new_keys},
                "disk_completed": {k: v for k, v in prev_completed.items() if k in new_keys},
                "closed_batches": {k: v for k, v in prev_closed.items() if k in new_keys},
                "frozen_disks": {k: v for k, v in prev_frozen.items() if k in new_keys},
            }
        )
        # Allow previously cancelled cards to be picked up again on Start.
        for cid, card in list(_cards.items()):
            if card.get("status") == "cancelled":
                _cards.pop(cid, None)
                _cancel_requested.discard(cid)
    _restore_disk_batches_from_folders(ssd1_path, ssd2_path, seed=batch)
    save_config(
        {
            "last_batch": batch,
            "mode": mode,
            "ssd1": ssd1_path,
            "ssd2": ssd2_path,
            "s3_uri": s3_uri.strip(),
        }
    )
    _persist_disk_state()
    _ensure_watcher()
    _ensure_watchdog()
    _pump_closed_uploads()
    _log_line(
        f"Session started: {batch} ({mode}) — hotplug armed "
        "(insert/remove SDs anytime; same batch number on both SSDs; 10 GB SSD reserve)"
    )
    # Immediately scan once
    _scan_for_cards()
    return get_status()


def stop_session() -> dict:
    with _lock:
        _session["active"] = False
    _log_line("Session stopped (hotplug disarmed; in-flight copies continue)")
    return get_status()


def cancel_card_job(card_id: str) -> dict:
    """Request cancel of an in-flight SD→SSD copy. Partial files on SSD are kept."""
    cid = str(card_id or "").strip().upper()
    if not cid:
        raise ValueError("card_id required")
    with _lock:
        # Drop from waiting queue if not started yet.
        before = len(_waiting_queue)
        _waiting_queue[:] = [j for j in _waiting_queue if str(j.get("card_id") or "").upper() != cid]
        removed_waiting = before != len(_waiting_queue)
        card = _cards.get(cid)
        if removed_waiting and card and card.get("status") == "waiting":
            card["status"] = "cancelled"
            card["message"] = "Cancelled while waiting for a free SD→SSD slot"
            card["speed_mbps"] = 0.0
            _log_line(f"{cid}: cancelled (was waiting)", kind="ok")
            _save_snapshot(force=True)
            return dict(card)
        if not card:
            raise RuntimeError(f"No active job for card {cid}")
        status = str(card.get("status") or "")
        if status == "cancelled":
            return dict(card)
        if status not in ACTIVE_COPY_STATUSES and status != "scanning":
            raise RuntimeError(f"Card {cid} is not cancellable ({status})")
        _cancel_requested.add(cid)
        card["status"] = "cancelling"
        card["message"] = "Cancel requested — stopping after current chunk…"
        card["speed_mbps"] = 0.0
        snap = dict(card)
    _log_line(f"{cid}: cancel requested", kind="ok")
    _save_snapshot(force=True)
    return snap


def retry_card_job(card_id: str) -> dict:
    """Resume a failed/interrupted/cancelled SD→SSD copy (skips files already on SSD)."""
    cid = str(card_id or "").strip().upper()
    if not cid:
        raise ValueError("card_id required")
    with _lock:
        card = _cards.get(cid)
        if not card:
            raise RuntimeError(f"No job for card {cid}")
        status = str(card.get("status") or "")
        if status not in MANUAL_RETRY_STATUSES:
            raise RuntimeError(f"Card {cid} is not retryable ({status})")
        mount = str(card.get("mount") or "")
        batch = str(_session.get("batch") or load_config().get("last_batch") or "")
        mode = str(_session.get("mode") or load_config().get("mode") or "ssd_and_aws")
        ssd1 = str(_session.get("ssd1") or load_config().get("ssd1") or "")
        ssd2 = str(_session.get("ssd2") or load_config().get("ssd2") or "")
        s3_uri = str(_session.get("s3_uri") or load_config().get("s3_uri") or "")
        _cancel_requested.discard(cid)
        card["status"] = "queued"
        card["message"] = "Retry queued…"
        card["error"] = ""
    if not batch:
        raise RuntimeError("No batch selected — Start SD → SSD first")
    if not mount or not Path(mount).exists():
        with _lock:
            if cid in _cards:
                _cards[cid]["status"] = "error"
                _cards[cid]["message"] = "Re-insert the SD card, then click Retry"
        raise RuntimeError("Card not mounted — re-insert it, then click Retry")
    if not ssd1 and not ssd2:
        raise RuntimeError("Pick SSD 1 / SSD 2 first")

    prog = progress.load_progress(Path(mount))
    _log_line(f"{cid}: retry requested — resume from progress file if present", kind="ok")
    _start_card_job(Path(mount), cid, batch, mode, ssd1, ssd2, s3_uri, prog)
    with _lock:
        return dict(_cards.get(cid) or {"card_id": cid, "status": "queued"})


def _is_cancel_requested(card_id: str) -> bool:
    with _lock:
        return card_id.upper() in _cancel_requested


def _clear_cancel_requested(card_id: str) -> None:
    with _lock:
        _cancel_requested.discard(card_id.upper())


def _ensure_watcher() -> None:
    global _watcher_started
    with _lock:
        if _watcher_started:
            return
        _watcher_started = True
    threading.Thread(target=_watcher_loop, daemon=True, name="sd-watcher").start()


def _ensure_watchdog() -> None:
    global _watchdog_started
    with _lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    threading.Thread(target=_watchdog_loop, daemon=True, name="offloader-watchdog").start()


def _watchdog_loop() -> None:
    try:
        run_watchdog_once()
    except Exception as exc:  # noqa: BLE001
        _log_line(f"Watchdog startup: {exc}", kind="error")
    while True:
        time.sleep(WATCHDOG_SECONDS)
        try:
            run_watchdog_once()
        except Exception as exc:  # noqa: BLE001
            _log_line(f"Watchdog: {exc}", kind="error")


def run_watchdog_once() -> None:
    """Resume interrupted work; never kill a healthy or still-progressing copy."""
    notes: list[str] = []
    with _lock:
        cards = [dict(c) for c in _cards.values()]
        threads = dict(_copy_threads)
    now = time.time()
    for card in cards:
        cid = str(card.get("card_id") or "").upper()
        if not cid:
            continue
        status = str(card.get("status") or "")
        thread = threads.get(cid)
        if thread is not None and thread.is_alive():
            if status in {"copying", "verifying"}:
                done = int(card.get("bytes_done") or 0)
                prev = _watchdog_copy_snap.get(cid)
                if prev is None or done > prev[0]:
                    _watchdog_copy_snap[cid] = (done, now)
                elif now - prev[1] >= WATCHDOG_SECONDS:
                    stalled_min = int((now - prev[1]) / 60)
                    _update_card(
                        cid,
                        message=(
                            f"Watchdog: copy stalled at {done} bytes for "
                            f"{stalled_min} min — still waiting, not killing"
                        ),
                    )
                    notes.append(f"SD {cid} stalled (bytes unchanged, thread alive)")
            continue
        _watchdog_copy_snap.pop(cid, None)
        stale_copy = (
            status in {"copying", "verifying"}
            and now - float(card.get("started_at") or 0) > 20
        )
        if status != "interrupted" and not stale_copy:
            continue
        mount = str(card.get("mount") or "")
        if not mount or not Path(mount).exists():
            continue
        try:
            if stale_copy:
                _update_card(
                    cid,
                    status="interrupted",
                    message="Watchdog: copy thread died — resuming",
                    speed_mbps=0.0,
                )
            retry_card_job(cid)
            notes.append(f"resumed SD {cid}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"SD {cid}: {exc}")
    try:
        notes.extend(aws_upload.watchdog_pass())
    except Exception as exc:  # noqa: BLE001
        notes.append(f"AWS pass: {exc}")
    try:
        _pump_closed_uploads()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"closed-batch upload queue: {exc}")
    for line in notes:
        _log_line(f"Watchdog: {line}", kind="ok")


def _watcher_loop() -> None:
    while True:
        try:
            with _lock:
                active = _session.get("active")
            if active:
                _scan_for_cards()
                time.sleep(1.0)  # hotplug: check inserts/removals every second
            else:
                time.sleep(2.0)
        except Exception as exc:  # noqa: BLE001
            _log_line(f"Watcher error: {exc}", kind="error")
            time.sleep(2.0)


def _scan_for_cards() -> None:
    with _lock:
        ssd1 = _session.get("ssd1") or ""
        ssd2 = _session.get("ssd2") or ""
        exclude = {p for p in (ssd1, ssd2) if p}
        batch = _session.get("batch") or ""
        mode = _session.get("mode") or "ssd_and_aws"
        s3_uri = _session.get("s3_uri") or ""

    cards = find_card_volumes(exclude_paths=exclude)
    present: dict[str, dict] = {}
    for vol in cards:
        cid = _resolve_card_id(vol)
        if cid:
            present[cid] = vol

    _reconcile_hotplug(present)

    for vol in cards:
        card_id = _resolve_card_id(vol)
        if not card_id:
            _log_line(
                f"Skipping volume {vol.get('path')}: could not assign a tracking id "
                "(still has DCIM/xxxGOPRO media — check mount path)",
                kind="error",
            )
            continue
        card_root = Path(vol["path"])
        serial = str(vol.get("volume_serial") or "")
        with _lock:
            existing = _cards.get(card_id)
            thread = _copy_threads.get(card_id)
            thread_alive = bool(thread and thread.is_alive())

            # New physical media under the same tracking id → clear old job and auto-start.
            if existing and serial:
                old_serial = str(existing.get("volume_serial") or "")
                if old_serial and old_serial != serial:
                    if existing.get("status") in MANUAL_RETRY_STATUSES | HOTPLUG_CLEAR_STATUSES | {
                        "cancelling",
                        "waiting",
                    }:
                        _log_line(
                            f"{card_id}: new SD media detected on hotplug "
                            f"(serial {old_serial} → {serial}) — auto-starting",
                            kind="ok",
                        )
                        _waiting_queue[:] = [
                            j
                            for j in _waiting_queue
                            if str(j.get("card_id") or "").upper() != card_id
                        ]
                        _cancel_requested.discard(card_id)
                        _cards.pop(card_id, None)
                        existing = None
                        thread_alive = False

            # Removed / finished cards that were unplugged — ready for next insert.
            if existing and existing.get("status") == "removed":
                _cards.pop(card_id, None)
                existing = None

            # Operator cancelled / failed — do not auto-restart until Retry
            # (unless serial change already cleared the row above).
            if existing and existing.get("status") in MANUAL_RETRY_STATUSES | {"cancelling"}:
                continue
            if existing and existing.get("status") in ACTIVE_COPY_STATUSES and thread_alive:
                if existing.get("mount") == str(card_root):
                    continue
            # Finished this session. Empty wiped card stays DONE. New files on a
            # still-mounted completed card → re-offload. Unplugged completed cards
            # were moved to "removed" by reconcile.
            if existing and existing.get("status") in {
                "completed",
                "wiping",
                "ejecting",
                "uploading",
            }:
                if not inventory.list_transfer_files(card_root):
                    if existing.get("status") != "completed" and not thread_alive:
                        existing["status"] = "completed"
                        existing["message"] = "Ready — card finished (hotplug armed for next SD)"
                    continue
                if existing.get("status") != "completed":
                    continue
            if existing and existing.get("status") in ACTIVE_COPY_STATUSES and not thread_alive:
                if existing.get("status") == "cancelling":
                    existing["status"] = "cancelled"
                    existing["message"] = "Cancelled"
                    continue
                if existing.get("status") == "waiting":
                    continue
                existing["status"] = "interrupted"
                existing["message"] = (
                    "Copy worker stopped — click Retry to resume "
                    "(completed files on SSD are skipped)"
                )
                _log_line(f"{card_id}: interrupted — waiting for Retry", kind="error")
                continue

        prog = progress.load_progress(card_root)
        if prog and prog.get("status") == "complete" and prog.get("batch") == batch:
            dest_hint = Path(str(prog.get("dest") or ""))
            if dest_hint.is_dir() and progress.dest_looks_complete(prog, dest_hint):
                # Only skip if this looks like the same finished media still plugged in.
                with _lock:
                    prev = _cards.get(card_id) or {}
                    same_serial = (
                        not serial
                        or not prev.get("volume_serial")
                        or str(prev.get("volume_serial")) == serial
                    )
                    if same_serial and prev.get("status") == "completed":
                        continue
                    _cards[card_id] = {
                        "card_id": card_id,
                        "mount": str(card_root),
                        "volume_serial": serial,
                        "status": "completed",
                        "message": f"Already on SSD: {dest_hint} — SD card not wiped",
                        "dest": str(dest_hint),
                        "bytes_done": prog.get("bytes_total") or 0,
                        "bytes_total": prog.get("bytes_total") or 0,
                        "speed_mbps": 0,
                        "eta_seconds": 0,
                        "started_at": time.time(),
                    }
                continue
            _log_line(
                f"{card_id}: progress file says complete but SSD folder empty/incomplete — re-copying",
                kind="error",
            )
            progress.clear_progress(card_root)
            prog = None

        _start_card_job(
            card_root, card_id, batch, mode, ssd1, ssd2, s3_uri, prog, volume_serial=serial
        )
    _pump_waiting_queue()


def _reconcile_hotplug(present: dict[str, dict]) -> None:
    """Detect SD removals while the session stays armed for the next insert."""
    with _lock:
        for cid, card in list(_cards.items()):
            status = str(card.get("status") or "")
            if status == "removed":
                continue
            if cid in present:
                continue
            mount = str(card.get("mount") or "")
            mount_alive = False
            if mount:
                try:
                    mount_alive = Path(mount).exists()
                except OSError:
                    mount_alive = False
            if mount_alive:
                continue

            # Drop waiting entries for this card.
            _waiting_queue[:] = [
                j for j in _waiting_queue if str(j.get("card_id") or "").upper() != cid
            ]

            if status == "waiting":
                card["status"] = "removed"
                card["message"] = (
                    "Card removed from wait queue — insert anytime to auto-start "
                    "(session still armed)"
                )
                card["speed_mbps"] = 0.0
                _log_line(f"{cid}: removed while waiting", kind="ok")
            elif status in {"queued", "copying", "verifying", "cancelling", "scanning"}:
                # Mid SD→SSD transfer only — ask for Retry; do not auto-restart.
                _cancel_requested.add(cid)
                card["status"] = "interrupted"
                card["message"] = (
                    "SD removed during transfer — re-insert the same card and click Retry "
                    "(SSD files already copied are kept; no duplicate upload)"
                )
                card["speed_mbps"] = 0.0
                _log_line(f"{cid}: hotplug remove during {status}", kind="error")
            elif status in {"wiping", "ejecting", "uploading", "completed"}:
                # SSD copy already verified — keep AWS/session going; slot ready for next SD.
                card["status"] = "removed"
                card["message"] = (
                    "Card removed — insert the next SD anytime "
                    "(same batch stays active; no Start needed)"
                )
                _log_line(f"{cid}: removed after finish — waiting for next card", kind="ok")
            elif status in MANUAL_RETRY_STATUSES:
                card["status"] = "removed"
                card["message"] = (
                    "Card removed — plug a new SD to auto-start, "
                    "or re-insert and Retry if this was a failed job"
                )
                _log_line(f"{cid}: removed while {status}", kind="ok")
    _save_snapshot(force=False)


def _resolve_card_id(vol: dict) -> str:
    """Tracking id from detect (volume serial) — never requires a C#### label."""
    raw = (vol.get("card_id") or "").strip().upper()
    if raw:
        return raw
    serial = str(vol.get("volume_serial") or "").strip().upper()
    if serial:
        return f"SD-{serial[-8:]}" if len(serial) >= 4 else f"SD-{serial}"
    path = Path(str(vol.get("path") or ""))
    drive = path.drive.rstrip(":\\/") if path.drive else ""
    if drive:
        return f"SD-{drive.upper()}"
    anchor = path.anchor.replace("\\", "").replace(":", "").replace("/", "").upper()
    if anchor:
        return f"SD-{anchor}"
    return ""


def _active_copy_count() -> int:
    with _lock:
        return sum(1 for t in _copy_threads.values() if t and t.is_alive())


def _ssd_holding_path(dest: Path, ssd1: str, ssd2: str) -> str:
    """Return which configured SSD contains dest, or ''."""
    try:
        dest_key = space.path_key(dest)
    except OSError:
        dest_key = str(dest).lower()
    for raw in (ssd1, ssd2):
        if not raw:
            continue
        try:
            root = space.path_key(raw)
        except OSError:
            root = str(raw).lower()
        if dest_key == root or dest_key.startswith(root.rstrip("\\/") + "\\") or dest_key.startswith(
            root.rstrip("/") + "/"
        ):
            return str(Path(raw).expanduser().resolve())
    return ""


def _committed_bytes_by_ssd(*, exclude_card: str = "") -> dict[str, int]:
    """Remaining SD→SSD bytes already assigned to each SSD (not yet on disk)."""
    exclude = exclude_card.upper()
    committed: dict[str, int] = {}

    def _add(ssd: str, nbytes: int) -> None:
        if not ssd or nbytes <= 0:
            return
        try:
            key = space.path_key(ssd)
        except OSError:
            key = str(ssd).lower()
        committed[key] = committed.get(key, 0) + int(nbytes)

    with _lock:
        rows = list(_cards.values())
    for card in rows:
        if str(card.get("card_id") or "").upper() == exclude:
            continue
        status = str(card.get("status") or "")
        ssd = str(card.get("ssd") or "")
        total = int(card.get("bytes_total") or 0)
        done = int(card.get("bytes_done") or 0)
        if status in {"waiting", "queued"}:
            _add(ssd, total)
        elif status in {"copying", "scanning"}:
            _add(ssd, max(0, total - done))
    return committed


def _persist_disk_state() -> None:
    """Write live/completed/closed/frozen batch maps so a restart can resume."""
    with _lock:
        payload = {
            "disk_batches": dict(_session.get("disk_batches") or {}),
            "disk_completed": dict(_session.get("disk_completed") or {}),
            "closed_batches": dict(_session.get("closed_batches") or {}),
            "frozen_disks": dict(_session.get("frozen_disks") or {}),
        }
    save_config(payload)
    _save_snapshot(force=True)


def _ssd_key_from_batch_folder(path: str | Path, ssd1: str, ssd2: str) -> str:
    """Map ``E:\\Batches\\batch01`` back to the SSD path_key that owns it."""
    resolved = Path(path).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:
        pass
    for ssd in (ssd1, ssd2):
        if not ssd:
            continue
        root = Path(ssd).expanduser()
        try:
            root = root.resolve()
        except OSError:
            pass
        try:
            resolved.relative_to(root)
            return space.path_key(root)
        except ValueError:
            continue
    if resolved.parent.name.lower() == "batches":
        return space.path_key(resolved.parent.parent)
    return space.path_key(resolved)


def _resolve_ssd_arg(ssd: str) -> str:
    """Map UI values ``1`` / ``2`` / a path onto the session SSD path."""
    raw = (ssd or "").strip()
    with _lock:
        ssd1 = str(_session.get("ssd1") or "")
        ssd2 = str(_session.get("ssd2") or "")
    lowered = raw.lower().replace(" ", "")
    if lowered in {"1", "ssd1"}:
        if not ssd1:
            raise ValueError("SSD 1 is not set — Start auto offload first")
        return ssd1
    if lowered in {"2", "ssd2"}:
        if not ssd2:
            raise ValueError("SSD 2 is not set — Start auto offload first")
        return ssd2
    if not raw:
        raise ValueError("SSD required")
    path = str(Path(raw).expanduser().resolve())
    known = {space.path_key(p): p for p in (ssd1, ssd2) if p}
    key = space.path_key(path)
    if known and key not in known:
        raise ValueError("SSD does not match SSD 1 or SSD 2 for this session")
    return known.get(key, path)


def _closed_names(key: str) -> list[str]:
    with _lock:
        raw = (_session.get("closed_batches") or {}).get(key) or []
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = str(item or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _folder_has_transfer_files(folder: Path) -> bool:
    try:
        if not folder.is_dir():
        return False
        return any(folder.iterdir())
    except OSError:
        return False


def _aws_status_for_folder(ssd_path: str, batch_name: str) -> str:
    """Map AWS jobs for this SSD folder: waiting / uploading / verifying / verified / failed."""
    folder = str(space.batch_root(ssd_path, batch_name))
    folder_l = folder.replace("\\", "/").rstrip("/").lower()
    jobs = aws_upload.list_jobs()
    matched: dict | None = None
    for job in jobs:
        if str(job.get("batch") or "").strip() != batch_name:
            continue
        sources = [str(s).replace("\\", "/").rstrip("/").lower() for s in (job.get("sources") or [])]
        if folder_l in sources or any(folder_l in s or s in folder_l for s in sources):
            matched = job
            break
    if not matched:
        return "waiting"
    status = str(matched.get("status") or "")
    if status in {"running", "checking", "cancelling"}:
        return "uploading"
    if status == "completed":
        return "verifying"
    if status == "verified":
        return "verified"
    if status == "deleted_local":
        return "cleaned"
    if status in {"error", "mismatch", "interrupted", "cancelled"}:
        return "failed"
    return status or "waiting"


def _ssd_has_running_upload(ssd_path: str) -> bool:
    key = space.path_key(ssd_path)
    for name in _closed_names(key):
        if _aws_status_for_folder(ssd_path, name) == "uploading":
            return True
    return False


def _start_closed_batch_upload(ssd_path: str, batch_name: str) -> dict | None:
    with _lock:
        mode = str(_session.get("mode") or "")
        s3_uri = str(_session.get("s3_uri") or "")
        ssd1 = str(_session.get("ssd1") or "")
        ssd2 = str(_session.get("ssd2") or "")
    if mode != "ssd_and_aws" or not s3_uri:
        return None
    folder = space.batch_root(ssd_path, batch_name)
    if not _folder_has_transfer_files(folder):
        return None
    key = space.path_key(ssd_path)
    only1 = ssd_path if ssd1 and space.path_key(ssd1) == key else ""
    only2 = ssd_path if ssd2 and space.path_key(ssd2) == key else ""
    if not only1 and not only2:
        only1 = ssd_path
        only2 = ""
    job = aws_upload.start_batch_upload(
        s3_uri=s3_uri,
        batch_name=batch_name,
        ssd1=only1 or (ssd_path if not only2 else ""),
        ssd2=only2,
        card_id=None,
        auto_delete=True,
    )
    with _lock:
        frozen = dict(_session.get("frozen_disks") or {})
        frozen[key] = batch_name
        _session["frozen_disks"] = frozen
    _persist_disk_state()
    return job


def _pump_closed_uploads() -> None:
    """Start at most one AWS upload per SSD for the oldest closed batch that needs it."""
    with _lock:
        ssd1 = str(_session.get("ssd1") or "")
        ssd2 = str(_session.get("ssd2") or "")
        mode = str(_session.get("mode") or "")
    if mode != "ssd_and_aws":
        return
    for ssd_path in (ssd1, ssd2):
        if not ssd_path:
            continue
        if _ssd_has_running_upload(ssd_path):
            continue
        key = space.path_key(ssd_path)
        for name in _closed_names(key):
            st = _aws_status_for_folder(ssd_path, name)
            if st in {"uploading", "verifying", "verified", "cleaned", "failed"}:
                continue
            try:
                job = _start_closed_batch_upload(ssd_path, name)
            except Exception as exc:  # noqa: BLE001
                _log_line(f"{ssd_path}: could not upload closed {name} — {exc}", kind="error")
                continue
            if job:
                _log_line(f"Queued AWS for closed {name} on {ssd_path}", kind="ok")
                break


def _disk_batch_states() -> dict:
    """UI payload: active vs closed batches and AWS lifecycle per SSD."""
    with _lock:
        ssd1 = str(_session.get("ssd1") or "")
        ssd2 = str(_session.get("ssd2") or "")
        live = dict(_session.get("disk_batches") or {})
    rows = {}
    for label, path in (("ssd1", ssd1), ("ssd2", ssd2)):
        if not path:
            continue
        key = space.path_key(path)
        active = str(live.get(key) or "")
        closed = _closed_names(key)
        items = []
        for name in closed:
            aws = _aws_status_for_folder(path, name)
            items.append(
                {
                    "name": name,
                    "role": "closed",
                    "aws": aws,
                    "state": aws or "waiting",
                }
            )
        if active and active not in closed:
            items.append({"name": active, "role": "active", "aws": "", "state": "active"})
        items.sort(key=lambda row: (batches.batch_number(row["name"]) or 0, row["name"].lower()))
        rows[label] = {
            "path": path,
            "active": active,
            "batches": items,
        }
    return rows


def close_active_batch_for_ui(ssd: str) -> dict:
    """UI entry: close the active batch on SSD 1 / SSD 2 / a path."""
    return close_active_batch(_resolve_ssd_arg(ssd))


def close_active_batch(ssd_path: str) -> dict:
    """Close the active batch for new offload and open the next cycle number.

    Does not upload-complete or delete the old folder. AWS can catch up later.
    """
    if not ssd_path:
        raise ValueError("SSD path required")
    path = str(Path(ssd_path).expanduser().resolve())
    key = space.path_key(path)
    with _lock:
        ssd1 = str(_session.get("ssd1") or "")
        ssd2 = str(_session.get("ssd2") or "")
        seed = str(_session.get("batch") or load_config().get("last_batch") or "batch01")
        current = str((_session.get("disk_batches") or {}).get(key) or "")
    known = {space.path_key(p) for p in (ssd1, ssd2) if p}
    if known and key not in known:
        raise ValueError("SSD does not match SSD 1 or SSD 2 for this session")
    if not current:
        current = _batch_for_ssd(path, seed=seed)
    closed = _closed_names(key)
    if current in closed:
        nxt = batches.successor_batch_name(current, seed=seed)
        with _lock:
            disk_batches = dict(_session.get("disk_batches") or {})
            disk_batches[key] = nxt
            _session["disk_batches"] = disk_batches
        space.batch_root(path, nxt).mkdir(parents=True, exist_ok=True)
        _persist_disk_state()
        return {
            "ok": True,
            "ssd": path,
            "closed": current,
            "active": nxt,
            "closed_batches": closed,
        }
    folder = space.batch_root(path, current)
    if not _folder_has_transfer_files(folder):
        raise ValueError(
            f"{current} has no footage yet — add SD cards first "
            "(Batch Completed does not delete; it only stops new cards on this batch)"
        )
    closed.append(current)
    nxt = batches.successor_batch_name(current, seed=seed)
    with _lock:
        disk_batches = dict(_session.get("disk_batches") or {})
        closed_map = dict(_session.get("closed_batches") or {})
        disk_batches[key] = nxt
        closed_map[key] = closed
        _session["disk_batches"] = disk_batches
        _session["closed_batches"] = closed_map
    space.batch_root(path, nxt).mkdir(parents=True, exist_ok=True)
    _persist_disk_state()
    _log_line(f"{path}: {current} closed for offload — {nxt} is now active", kind="ok")
    push_notice(f"{current} closed — now offloading to {nxt}", kind="ok")
    _pump_closed_uploads()
    return {
        "ok": True,
        "ssd": path,
        "closed": current,
        "active": nxt,
        "closed_batches": closed,
    }


def _restore_disk_batches_from_folders(ssd1: str, ssd2: str, *, seed: str) -> None:
    """Keep the active cycle; do not reopen a closed folder as the live batch."""
    for path in (ssd1, ssd2):
        if not path:
            continue
        key = space.path_key(path)
        with _lock:
            live = str((_session.get("disk_batches") or {}).get(key) or "")
            completed = str((_session.get("disk_completed") or {}).get(key) or "")
        closed = set(_closed_names(key))
        if live and live not in closed and space.batch_root(path, live).exists():
            continue
        folders = batches.used_batch_names(path, "")
        open_folders = {n for n in folders if n not in closed}
        if open_folders:
            name = max(
                open_folders,
                key=lambda n: (batches.batch_number(n) or 0, n.lower()),
            )
        else:
            name = batches.resume_or_next_batch(
                seed=seed, live="", completed=completed, folders=set()
            )
            if closed:
                last_closed = max(
                    closed, key=lambda n: (batches.batch_number(n) or 0, n.lower())
                )
                cand = batches.successor_batch_name(last_closed, seed=seed)
                if (batches.batch_number(cand) or 0) >= (batches.batch_number(name) or 0):
                    name = cand
        with _lock:
            disk_batches = dict(_session.get("disk_batches") or {})
            disk_batches[key] = name
            _session["disk_batches"] = disk_batches


def _is_frozen(ssd_path: str) -> bool:
    """Uploading a closed batch must not block new cards on this SSD."""
    del ssd_path
    return False


def _live_batches() -> set[str]:
    with _lock:
        values = (_session.get("disk_batches") or {}).values()
    return {str(v) for v in values if v}


def _other_ssd(ssd_path: str, ssd1: str, ssd2: str) -> str:
    if not ssd_path:
        return ""
    key = space.path_key(ssd_path)
    if ssd1 and space.path_key(ssd1) == key:
        return ssd2
    if ssd2 and space.path_key(ssd2) == key:
        return ssd1
    return ""


def _batch_for_ssd(ssd_path: str, *, seed: str) -> str:
    """Same cycle number on both SSDs; closed folders are not reused as active."""
    key = space.path_key(ssd_path)
    with _lock:
        disk_batches = dict(_session.get("disk_batches") or {})
        completed = str((_session.get("disk_completed") or {}).get(key) or "")
        current = str(disk_batches.get(key) or "")
    closed = set(_closed_names(key))
    if current in closed:
        current = ""
    folders = {n for n in batches.used_batch_names(ssd_path, "") if n not in closed}
    name = batches.resume_or_next_batch(
        seed=seed, live=current, completed=completed, folders=folders
    )
    if closed and not current:
        last_closed = max(closed, key=lambda n: (batches.batch_number(n) or 0, n.lower()))
        cand = batches.successor_batch_name(last_closed, seed=seed)
        if (batches.batch_number(cand) or 0) > (batches.batch_number(name) or 0):
            name = cand
    with _lock:
        disk_batches = dict(_session.get("disk_batches") or {})
        disk_batches[key] = name
        _session["disk_batches"] = disk_batches
    space.batch_root(ssd_path, name).mkdir(parents=True, exist_ok=True)
    if name != current:
    _log_line(f"Opened {name} on {ssd_path}")
        _persist_disk_state()
    return name


def _maybe_auto_upload_disk(ssd_path: str) -> None:
    """SSD+AWS: when this disk cannot take the next card, close the active batch for offload."""
    with _lock:
        mode = str(_session.get("mode") or "")
        s3_uri = str(_session.get("s3_uri") or "")
        if mode != "ssd_and_aws" or not s3_uri or not ssd_path:
            return
        key = space.path_key(ssd_path)
        batch = str((_session.get("disk_batches") or {}).get(key) or "")
    if not batch or batch in _closed_names(key):
        return
    folder = space.batch_root(ssd_path, batch)
    if not _folder_has_transfer_files(folder):
        return
    try:
        close_active_batch(ssd_path)
    except Exception as exc:  # noqa: BLE001
        _log_line(f"Auto-close {batch} on {ssd_path} failed — {exc}", kind="error")


def _ssd_path_for_key(key: str, ssd1: str, ssd2: str, ssd_paths: list[str]) -> str:
    """Map a path_key back to the session SSD path (or the deleted folder's disk)."""
    for path in (ssd1, ssd2):
        if path and space.path_key(path) == key:
            return path
    for raw in ssd_paths or []:
        if not raw:
            continue
        if _ssd_key_from_batch_folder(raw, ssd1, ssd2) != key:
            continue
        folder = Path(raw)
        try:
            folder = folder.expanduser().resolve()
    except OSError:
            pass
        if folder.parent.name.lower() == "batches":
            return str(folder.parent.parent)
        if folder.name.lower() == "batches":
            return str(folder.parent)
        return str(folder)
    return ""


def _ensure_next_active_batch(ssd_path: str, *, deleted: str) -> str | None:
    """After AWS delete, keep a newer live batch or open the successor folder."""
    if not ssd_path:
        return None
    key = space.path_key(ssd_path)
    with _lock:
        seed = str(_session.get("batch") or load_config().get("last_batch") or "batch01")
        live = str((_session.get("disk_batches") or {}).get(key) or "")
    if live and live != deleted:
        try:
            space.batch_root(ssd_path, live).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return live
    if Path(ssd_path).exists():
        return _batch_for_ssd(ssd_path, seed=seed)
    nxt = batches.successor_batch_name(deleted, seed=seed)
        with _lock:
        disk_batches = dict(_session.get("disk_batches") or {})
        disk_batches[key] = nxt
        _session["disk_batches"] = disk_batches
    return nxt


def on_batch_deleted(ssd_paths: list[str], batch_name: str) -> None:
    """Unfreeze only the SSD(s) whose batch folder was deleted after verify."""
    with _lock:
        ssd1 = str(_session.get("ssd1") or "")
        ssd2 = str(_session.get("ssd2") or "")
    keys: set[str] = set()
    for raw in ssd_paths or []:
        if raw:
            keys.add(_ssd_key_from_batch_folder(raw, ssd1, ssd2))
    with _lock:
        frozen = dict(_session.get("frozen_disks") or {})
        disk_batches = dict(_session.get("disk_batches") or {})
        completed = dict(_session.get("disk_completed") or {})
        if not keys:
            keys = {
                key
                for key, name in frozen.items()
                if name == batch_name
            }
        for key in list(keys):
            for existing in list(disk_batches) + list(frozen) + list(completed) + list(
                (_session.get("closed_batches") or {}).keys()
            ):
                if existing.lower() == key.lower() or space.path_key(existing) == key:
                    keys.add(existing)
        closed_map = dict(_session.get("closed_batches") or {})
        for key in keys:
            names = [n for n in (closed_map.get(key) or []) if str(n) != batch_name]
            if names:
                closed_map[key] = names
            else:
                closed_map.pop(key, None)
            if disk_batches.get(key) == batch_name:
                disk_batches.pop(key, None)
            if frozen.get(key) == batch_name:
                frozen.pop(key, None)
            prev = str(completed.get(key) or "")
            if (batches.batch_number(batch_name) or 0) >= (batches.batch_number(prev) or 0):
                completed[key] = batch_name
        _session["closed_batches"] = closed_map
        _session["frozen_disks"] = frozen
        _session["disk_batches"] = disk_batches
        _session["disk_completed"] = completed
        affected_keys = set(keys)
    _persist_disk_state()
    opened: list[str] = []
    for key in affected_keys:
        path = _ssd_path_for_key(key, ssd1, ssd2, ssd_paths or [])
        try:
            nxt = _ensure_next_active_batch(path, deleted=batch_name)
        except Exception as exc:  # noqa: BLE001
            _log_line(f"{path or key}: could not open next batch after {batch_name} — {exc}", kind="error")
            continue
        if nxt:
            opened.append(nxt)
    _persist_disk_state()
    nxt_note = f" — {opened[0]} is active for new offload" if len(opened) == 1 else ""
    _log_line(f"Deleted verified {batch_name} — that disk is free for the next cycle{nxt_note}", kind="ok")
    push_notice(
        f"{batch_name} verified on AWS and deleted locally"
        + (f" — now offloading to {opened[0]}" if len(opened) == 1 else ""),
        kind="ok",
    )
    _pump_closed_uploads()


def _assign_ssd_and_batch(
    *,
    needed: int,
    ssd1: str,
    ssd2: str,
    seed: str,
    exclude_card: str,
    resume_dest: Path | None,
) -> tuple[str, Path, str]:
    """Pick one SSD (10 GB reserve, skip frozen); both disks share the cycle number."""
    if resume_dest:
        ssd_path = _ssd_holding_path(resume_dest, ssd1, ssd2)
        if ssd_path:
            batch = resume_dest.name if resume_dest.name else _batch_for_ssd(ssd_path, seed=seed)
            key = space.path_key(ssd_path)
            if batch not in _closed_names(key):
            with _lock:
                disk_batches = dict(_session.get("disk_batches") or {})
                    disk_batches[key] = batch
                _session["disk_batches"] = disk_batches
            dest = space.batch_root(ssd_path, batch)
            dest.mkdir(parents=True, exist_ok=True)
            return ssd_path, dest, batch

    reserved = _committed_bytes_by_ssd(exclude_card=exclude_card)

    # Prefer SSD1 when the card fits (uploading a closed batch does not block).
    picked = ""
    if ssd1 and not _is_frozen(ssd1):
        try:
            space.pick_ssd_for_bytes(
                ssd1=ssd1, ssd2="", needed_bytes=needed, reserved_bytes=reserved
            )
            picked = ssd1
        except RuntimeError:
            _maybe_auto_upload_disk(ssd1)
    if not picked and ssd2 and not _is_frozen(ssd2):
        try:
            space.pick_ssd_for_bytes(
                ssd1="", ssd2=ssd2, needed_bytes=needed, reserved_bytes=reserved
            )
            picked = ssd2
        except RuntimeError:
            _maybe_auto_upload_disk(ssd2)
    if not picked:
        raise RuntimeError(
            "No SSD can take this card with 10 GB reserve "
            "(free space, or wait for a closed batch to finish AWS cleanup)"
        )
    batch = _batch_for_ssd(picked, seed=seed)
    dest = space.batch_root(picked, batch)
    dest.mkdir(parents=True, exist_ok=True)
    return picked, dest, batch


def _pump_waiting_queue() -> None:
    """Start waiting card jobs when a parallel slot frees up."""
    while True:
        if _active_copy_count() >= _max_parallel_cards():
            return
        with _lock:
            if not _waiting_queue:
                return
            job = _waiting_queue.pop(0)
        try:
            _launch_copy_thread(
                card_root=job["card_root"],
                card_id=job["card_id"],
                batch=job["batch"],
                mode=job["mode"],
                s3_uri=job["s3_uri"],
                files=job["files"],
                dest=job["dest"],
                prog=job["prog"],
                ssd_path=job["ssd_path"],
                total=job["total"],
                volume_serial=str(job.get("volume_serial") or ""),
                reader_slot=str(job.get("reader_slot") or ""),
                reader_label=str(job.get("reader_label") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            cid = str(job.get("card_id") or "?")
            _update_card(cid, status="error", message=str(exc))
            _log_line(f"{cid}: failed to start from wait queue — {exc}", kind="error")


def _start_card_job(
    card_root: Path,
    card_id: str,
    batch: str,
    mode: str,
    ssd1: str,
    ssd2: str,
    s3_uri: str,
    existing_progress: dict | None,
    *,
    volume_serial: str = "",
) -> None:
    files = inventory.list_transfer_files(card_root)
    total = inventory.total_bytes(files)
    if not files:
        with _lock:
            existing = _cards.get(card_id)
            if existing and existing.get("status") == "completed":
                return
        _log_line(f"{card_id}: no MP4s under DCIM/…GOPRO", kind="error")
        with _lock:
            _cards[card_id] = {
                "card_id": card_id,
                "mount": str(card_root),
                "volume_serial": volume_serial,
                "status": "error",
                "message": "No MP4s + JSON under DCIM/xxxGOPRO — click Retry after fixing",
                "bytes_done": 0,
                "bytes_total": 0,
                "speed_mbps": 0,
                "eta_seconds": None,
                "started_at": time.time(),
            }
        return

    missing = inventory.unpaired_mp4s(files)
    if missing:
        preview = ", ".join(missing[:6]) + ("…" if len(missing) > 6 else "")
        msg = f"MP4 missing JSON sidecar: {preview}"
        _log_line(f"{card_id}: {msg}", kind="error")
        with _lock:
            _cards[card_id] = {
                "card_id": card_id,
                "mount": str(card_root),
                "volume_serial": volume_serial,
                "status": "error",
                "message": f"{msg} — click Retry after labeling",
                "bytes_done": 0,
                "bytes_total": total,
                "speed_mbps": 0,
                "eta_seconds": None,
                "started_at": time.time(),
            }
        return

    try:
        ssd_path = ""
        dest: Path | None = None
        batch_name = batch
        ident = readers.match_reader(str(card_root))
        reader_slot = str(ident.get("slot") or "")
        reader_label = str(ident.get("label") or "")
        resume_dest = None
        if existing_progress:
            prev = Path(str(existing_progress.get("dest") or ""))
            if prev:
                resume_dest = prev
        ssd_path, dest, batch_name = _assign_ssd_and_batch(
            needed=total,
            ssd1=ssd1,
            ssd2=ssd2,
            seed=batch,
            exclude_card=card_id,
            resume_dest=resume_dest,
        )
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        _log_line(f"{card_id}: {exc}", kind="error")
        with _lock:
            _cards[card_id] = {
                "card_id": card_id,
                "mount": str(card_root),
                "volume_serial": volume_serial,
                "status": "error",
                "message": f"{exc} — click Retry when an SSD has space",
                "bytes_done": 0,
                "bytes_total": total,
                "speed_mbps": 0,
                "eta_seconds": None,
                "started_at": time.time(),
            }
        return

    prog = existing_progress or {
        "batch": batch_name,
        "card_id": card_id,
        "dest": str(dest),
        "files": {},
        "status": "in_progress",
        "bytes_total": total,
    }
    prog.update(
        {
            "batch": batch_name,
            "card_id": card_id,
            "dest": str(dest),
            "status": "in_progress",
            "bytes_total": total,
        }
    )
    progress.save_progress(card_root, prog)

    payload = {
        "card_root": card_root,
        "card_id": card_id,
        "batch": batch_name,
        "mode": mode,
        "s3_uri": s3_uri,
        "files": files,
        "dest": dest,
        "prog": prog,
        "ssd_path": ssd_path,
        "total": total,
        "volume_serial": volume_serial,
        "reader_slot": reader_slot,
        "reader_label": reader_label,
    }

    if _active_copy_count() >= _max_parallel_cards():
        with _lock:
            _waiting_queue[:] = [
                j
                for j in _waiting_queue
                if str(j.get("card_id") or "").upper() != card_id.upper()
            ]
            _waiting_queue.append(payload)
            active = _active_copy_count()
            limit = _max_parallel_cards()
            _cards[card_id] = {
                "card_id": card_id,
                "mount": str(card_root),
                "volume_serial": volume_serial,
                "status": "waiting",
                "message": f"Waiting for free slot ({active}/{limit} active) → {dest}",
                "dest": str(dest),
                "ssd": ssd_path,
                "reader_slot": payload.get("reader_slot") or "",
                "reader_label": payload.get("reader_label") or "",
                "bytes_done": 0,
                "bytes_total": total,
                "speed_mbps": 0.0,
                "eta_seconds": None,
                "files_total": len(files),
                "files_done": 0,
                "started_at": time.time(),
            }
        _log_line(
            f"{card_id}: waiting — {_max_parallel_cards()} cards already copying",
            kind="ok",
        )
        _save_snapshot(force=True)
        return

    _launch_copy_thread(**payload)


def _launch_copy_thread(
    *,
    card_root: Path,
    card_id: str,
    batch: str,
    mode: str,
    s3_uri: str,
    files: list[dict],
    dest: Path,
    prog: dict,
    ssd_path: str,
    total: int,
    volume_serial: str = "",
    reader_slot: str = "",
    reader_label: str = "",
) -> None:
    with _lock:
        _cards[card_id] = {
            "card_id": card_id,
            "mount": str(card_root),
            "volume_serial": volume_serial,
            "status": "queued",
            "message": f"Queued → {dest}",
            "dest": str(dest),
            "ssd": ssd_path,
            "reader_slot": reader_slot,
            "reader_label": reader_label,
            "bytes_done": 0,
            "bytes_total": total,
            "speed_mbps": 0.0,
            "eta_seconds": None,
            "files_total": len(files),
            "files_done": 0,
            "files_verified": 0,
            "started_at": time.time(),
        }

    _log_line(
        f"{card_id}: starting copy → {dest} ({len(files)} files, "
        f"{total / (1024**3):.2f} GB, 10 GB SSD reserve)"
    )
    thread = threading.Thread(
        target=_copy_card_worker,
        args=(card_root, card_id, batch, mode, s3_uri, files, dest, prog, reader_slot, reader_label),
        daemon=True,
        name=f"copy-{card_id}",
    )
    with _lock:
        _copy_threads[card_id] = thread
    thread.start()


def _update_card(card_id: str, **kwargs) -> None:
    with _lock:
        if card_id not in _cards:
            _cards[card_id] = {"card_id": card_id}
        _cards[card_id].update(kwargs)
    _save_snapshot(force=False)


def _flat_name(rel: str) -> str:
    """Batch destination is always a filename — never ``100GOPRO/…`` or a task folder."""
    text = str(rel or "").replace("\\", "/").strip().rstrip("/")
    if not text:
        return ""
    return Path(text).name


def _is_sidecar_rel(rel: str) -> bool:
    lower = _flat_name(rel).lower()
    return lower.endswith(".segments.json") or lower.endswith(".json")


def _pair_key(rel: str) -> str:
    """Stable per-source pairing key (folder + stem), not the flattened dest name."""
    text = str(rel or "").replace("\\", "/").strip()
    lower = text.lower()
    if lower.endswith(".segments.json"):
        return text[: -len(".segments.json")]
    return str(Path(text).with_suffix("")).replace("\\", "/")


def _sidecar_stem(rel: str) -> str:
    name = _flat_name(rel)
    lower = name.lower()
    if lower.endswith(".segments.json"):
        return name[: -len(".segments.json")]
    return Path(name).stem


def _name_taken(claimed: set[str], dest: Path, cand: str) -> bool:
    if cand.lower() in claimed:
        return True
    return (dest / cand).is_file() or (dest / f"{cand}.partial").is_file()


def _claim_flat_name(
    claimed: set[str], dest: Path, base: str, suffix: str, card_id: str, rel: str
) -> str:
    """Pick a unique filename: original, then ``base-1``, ``base-2``, … Never a subfolder.

    ``card_id`` / ``rel`` are unused (kept so call sites stay stable). Legacy
    ``stem__CARDID`` files already on disk still count as taken names.
    """
    del card_id, rel
    first = f"{base}{suffix}"
    if not _name_taken(claimed, dest, first):
        claimed.add(first.lower())
        return first
    n = 1
    while True:
        cand = f"{base}-{n}{suffix}"
        if not _name_taken(claimed, dest, cand):
            claimed.add(cand.lower())
            return cand
        n += 1


def _dest_batch_lock(dest: Path) -> threading.Lock:
    key = str(dest.resolve()).lower()
    with _lock:
        if key not in _batch_dest_locks:
            _batch_dest_locks[key] = threading.Lock()
        return _batch_dest_locks[key]


def _resolve_dest_names(files: list[dict], dest: Path, prog: dict, card_id: str) -> None:
    """Assign collision-safe destination names into the flat batch folder.

    Every file is ``Batches/<batch>/<filename>`` — never ``100GOPRO/`` or a
    task subfolder. GoPro numbering repeats across cards, so when another card
    already parked a *different* video under the same name, the incoming pair
    is renamed to ``<stem>-1<ext>``, ``<stem>-2<ext>``, … When the batch
    already holds the same video (sidecar/embed, or a hash only if metadata
    is missing), the existing name is reused and the card file is not copied
    or wiped.
    """
    entries = prog.get("files") or {}
    stem_map: dict[str, str] = {}  # pair key -> dest stem
    claimed: set[str] = set()
    already_pairs: set[str] = set()

    def assign_one(item: dict, *, sidecar_pass: bool) -> None:
        rel = item["rel"]
        recorded = _flat_name((entries.get(rel) or {}).get("dest_rel") or "")
        is_sidecar = _is_sidecar_rel(rel)
        if is_sidecar != sidecar_pass:
            return

        name = _flat_name(rel)
        pair = _pair_key(rel)
        base = _sidecar_stem(rel) if is_sidecar else Path(name).stem
        suffix = Path(name).suffix
        if _flat_name(rel).lower().endswith(".segments.json"):
            suffix = ".segments.json"

        if is_sidecar:
            final_base = _flat_name(stem_map.get(pair, base)) or base
            if recorded:
                item["dest_rel"] = recorded
            else:
                item["dest_rel"] = _dest_sidecar_name(dest, final_base)
            if pair in already_pairs:
                item["already_in_batch"] = True
            claimed.add(str(item["dest_rel"]).lower())
            return

        if recorded:
            item["dest_rel"] = recorded
            stem_map[pair] = Path(recorded).stem
            claimed.add(recorded.lower())
            return

        card_size = int(item.get("size") or 0)
        sidecar_payload = (
            pairing.load_sidecar(item["embed_json"]) if item.get("embed_json") else None
        )
        existing = pairing.find_existing_dest_name(
            dest,
            name,
            card_mp4_size=card_size,
            sidecar=sidecar_payload,
            card_mp4=item.get("source"),
        )
        flat_existing = _flat_name(existing) if existing else ""
        if existing and flat_existing.lower() not in claimed:
            item["dest_rel"] = flat_existing
            item["already_in_batch"] = True
            already_pairs.add(pair)
            stem_map[pair] = Path(flat_existing).stem
            claimed.add(flat_existing.lower())
            return

        dest_rel = _claim_flat_name(claimed, dest, base, suffix or Path(name).suffix, card_id, rel)
        item["dest_rel"] = dest_rel
        stem_map[pair] = Path(dest_rel).stem

    # MP4s first so sidecar rename can follow collision suffixes.
    for item in files:
        assign_one(item, sidecar_pass=False)
    for item in files:
        assign_one(item, sidecar_pass=True)


_META_CHECKS = (
    ("complete labeling", lambda p: p.get("complete") is True),
    ("segments", lambda p: bool(p.get("segments"))),
    ("device_id", lambda p: bool(p.get("device_id"))),
    ("device_type", lambda p: bool(p.get("device_type"))),
    ("camera_serial", lambda p: bool((p.get("media_meta") or {}).get("camera_serial"))),
    ("recorded_at", lambda p: bool((p.get("media_meta") or {}).get("recorded_at"))),
    ("IMU sensor list", lambda p: bool((p.get("media_meta") or {}).get("sensors"))),
)


def _missing_metadata(payload: dict) -> list[str]:
    """Names of expected sidecar fields that are absent/empty (for log warnings)."""
    return [name for name, present in _META_CHECKS if not present(payload)]


def _dest_sidecar_name(dest: Path, stem: str) -> str:
    """Sidecar filename that belongs to this dest MP4 stem.

    Reuse an existing ``.segments.json`` / ``.json`` next to the MP4; otherwise
    use the canonical ``{stem}.segments.json`` so every dest video has its own
    JSON through SSD and S3.
    """
    for ext in (".MP4", ".mp4"):
        mp4 = dest / f"{stem}{ext}"
        if mp4.is_file():
            found = inventory.sidecar_for_mp4(mp4)
            if found:
                return found.name
    return f"{stem}.segments.json"


def _ensure_dest_sidecar(dest_mp4: Path, card_sidecar: str | Path | None) -> Path | None:
    """Copy the card JSON beside the dest MP4 when that pair is missing."""
    dest_mp4 = Path(dest_mp4)
    existing = inventory.sidecar_for_mp4(dest_mp4)
    if existing:
        return existing
    if not card_sidecar:
        return None
    src = Path(card_sidecar)
    if not src.is_file():
        return None
    dest_side = dest_mp4.with_name(f"{dest_mp4.stem}.segments.json")
    copy_file(src, dest_side)
    return dest_side if dest_side.is_file() else None


def _preserve_dest_metadata(
    card_id: str,
    rel: str,
    src: Path,
    dest_mp4: Path,
    sidecar_path: str,
) -> int:
    """Keep JSON beside the dest MP4 and embed it when the MP4 has no payload.

    Card original is never modified. Existing dest sidecar/embed is left as-is.
    """
    dest_mp4 = Path(dest_mp4)
    src = Path(src)
    _ensure_dest_sidecar(dest_mp4, sidecar_path or None)
    try:
        dest_size = dest_mp4.stat().st_size
    except OSError:
        return 0
    if not sidecar_path:
        return dest_size
    if embed_meta.read_embedded_segments(dest_mp4):
        return dest_size
    if _is_cancel_requested(card_id):
        raise CopyCancelled(f"{card_id}: cancelled by operator")
    try:
        payload = pairing.load_sidecar(sidecar_path)
        if not payload:
            return dest_size
        mismatches = pairing.validate_sidecar_for_mp4(src, Path(sidecar_path), payload)
        if mismatches:
            _log_line(
                f"{card_id}: {rel} metadata mismatch — "
                f"{'; '.join(mismatches)} (sidecar copied; embed skipped)",
                kind="error",
            )
            return dest_size
        missing = _missing_metadata(payload)
        if missing:
            _log_line(
                f"{card_id}: {rel} sidecar is missing "
                f"{', '.join(missing)} — re-check in GoPro Cleaner",
                kind="error",
            )
        embed_meta.embed_segments_json(dest_mp4, payload)
        dest_size = dest_mp4.stat().st_size
        _log_line(f"{card_id}: embedded segments into {dest_mp4.name}")
    except CopyCancelled:
        raise
    except Exception as embed_exc:  # noqa: BLE001
        _log_line(
            f"{card_id}: could not embed segments into {rel} — {embed_exc}",
            kind="error",
        )
    return dest_size


def _copy_card_worker(
    card_root: Path,
    card_id: str,
    batch: str,
    mode: str,
    s3_uri: str,
    files: list[dict],
    dest: Path,
    prog: dict,
    reader_slot: str = "",
    reader_label: str = "",
) -> None:
    total_bytes = inventory.total_bytes(files)
    _update_card(
        card_id,
        status="copying",
        message=f"Copying → {dest}",
        dest=str(dest),
        bytes_done=0,
        bytes_total=total_bytes,
        files_done=0,
        files_total=len(files),
        files_verified=0,
        speed_mbps=0.0,
        eta_seconds=None,
    )
    started = time.time()
    done_bytes = 0
    files_done = 0
    files_verified = 0
    files_already = 0
    files_copied = 0
    with _dest_batch_lock(dest):
        _resolve_dest_names(files, dest, prog, card_id)
    last_ui = 0.0
    last_live = 0
    last_speed_at = started
    saw_disk_write = False

    def _publish(current_file_bytes: int = 0, *, message: str | None = None, force: bool = False) -> None:
        nonlocal last_ui, last_live, last_speed_at
        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")
        now = time.time()
        live = done_bytes + max(0, current_file_bytes)
        if not force and message is None and now - last_ui < 0.2:
            return
        last_ui = now
        elapsed = max(0.1, now - started)
        window = max(0.1, now - last_speed_at)
        delta = max(0, live - last_live)
        if delta > 0 and window >= 0.2:
            speed = (delta / (1024 * 1024)) / window
            last_live = live
            last_speed_at = now
        else:
            speed = (live / (1024 * 1024)) / elapsed if live > 0 else 0.0
        remaining = max(0, total_bytes - live)
        eta = int(remaining / (speed * 1024 * 1024)) if speed > 0 else None
        payload = {
            "status": "copying",
            "dest": str(dest),
            "bytes_done": live,
            "bytes_total": total_bytes,
            "files_done": files_done,
            "files_total": len(files),
            "files_verified": files_verified,
            "files_already_in_batch": files_already,
            "files_copied": files_copied,
            "speed_mbps": round(speed, 2),
            "eta_seconds": eta,
        }
        if message is not None:
            payload["message"] = message
        _update_card(card_id, **payload)

    try:
        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")
        for item in files:
            if _is_cancel_requested(card_id):
                raise CopyCancelled(f"{card_id}: cancelled by operator")
            rel = item["rel"]
            src = Path(item["source"])
            size = int(item["size"])
            dest_rel = _flat_name(item.get("dest_rel") or rel)
            item["dest_rel"] = dest_rel
            dest_file = dest / dest_rel
            if dest_rel != _flat_name(rel) and not (prog.get("files") or {}).get(rel):
                if item.get("already_in_batch"):
                    _log_line(
                        f"{card_id}: {_flat_name(rel)} already in this batch as {dest_rel} — not copied"
                    )
                else:
                    _log_line(
                        f"{card_id}: {_flat_name(rel)} already in batch from another card — saving as {dest_rel}"
                    )
            if item.get("already_in_batch") and dest_file.is_file():
                try:
                    item["dest_size"] = dest_file.stat().st_size
                except OSError:
                    item["dest_size"] = size
                item["copied"] = False
                if inventory._item_is_mp4(item):
                    item["dest_size"] = _preserve_dest_metadata(
                        card_id, rel, src, dest_file, item.get("embed_json") or ""
                    )
                    if item.get("embed_json") and inventory.sidecar_for_mp4(dest_file) is None:
                        raise RuntimeError(
                            f"JSON sidecar missing for dest MP4 {dest_file.name}"
                        )
                done_bytes += size
                files_done += 1
                files_already += 1
                _publish(
                    0,
                    message=f"Already in batch (not copied): {dest_rel} · {files_already} skipped",
                    force=True,
                )
                continue
            if progress.is_file_done(prog, rel, size, dest_file):
                try:
                    item["dest_size"] = dest_file.stat().st_size
                except OSError:
                    item["dest_size"] = size
                item["copied"] = True
                if inventory._item_is_mp4(item):
                    item["dest_size"] = _preserve_dest_metadata(
                        card_id, rel, src, dest_file, item.get("embed_json") or ""
                    )
                done_bytes += size
                files_done += 1
                saw_disk_write = True
                _publish(0, message=f"Skipped (done): {rel}", force=True)
                continue

            if not src.is_file():
                raise RuntimeError(f"Source missing on card: {src}")

            _publish(0, message=f"Copying {rel} → {dest_file}", force=True)

            def on_progress(written: int, _rel: str = dest_rel, _dest_file: Path = dest_file) -> None:
                nonlocal saw_disk_write
                if _is_cancel_requested(card_id):
                    raise CopyCancelled(f"{card_id}: cancelled by operator")
                partial = _dest_file.with_suffix(_dest_file.suffix + ".partial")
                if written > 0 and (partial.exists() or _dest_file.exists()):
                    saw_disk_write = True
                _publish(written, message=f"Copying {_rel}…")

            try:
                copy_file(src, dest_file, on_progress=on_progress)
            except CopyCancelled:
                # Drop incomplete .partial so resume doesn't confuse size checks.
                partial = dest_file.with_suffix(dest_file.suffix + ".partial")
                partial.unlink(missing_ok=True)
                raise
            if not dest_file.is_file() or dest_file.stat().st_size != size:
                raise RuntimeError(
                    f"Copy did not land on SSD: expected {dest_file} ({size} bytes)"
                )
            saw_disk_write = True

            # JSON beside the dest MP4 + embed inside it. Card original is never modified.
            dest_size = size
            if inventory._item_is_mp4(item):
                dest_size = _preserve_dest_metadata(
                    card_id, rel, src, dest_file, item.get("embed_json") or ""
                )
                if item.get("embed_json") and inventory.sidecar_for_mp4(dest_file) is None:
                    raise RuntimeError(
                        f"JSON sidecar missing for dest MP4 {dest_file.name}"
                    )
            item["dest_size"] = dest_size
            item["copied"] = True

            progress.mark_file_done(
                card_root, prog, rel, size, dest_size=dest_size, dest_rel=dest_rel
            )
            done_bytes += size
            files_done += 1
            files_copied += 1
            _publish(0, message=f"Copied {rel}", force=True)

        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")

        if not saw_disk_write and files and files_already < len(files):
            raise RuntimeError(
                f"No files were written under {dest} — check SSD path and card folders"
            )

        _update_card(card_id, status="verifying", message=f"Verifying {dest}…", dest=str(dest))
        manifest: list[dict] = []
        for item in files:
            if _is_cancel_requested(card_id):
                raise CopyCancelled(f"{card_id}: cancelled by operator")
            dest_rel = _flat_name(item.get("dest_rel") or item["rel"])
            dest_file = dest / dest_rel
            expected = int(item.get("dest_size") or item["size"])
            kind = str(item.get("kind") or "")
            if not kind:
                kind = (
                    "mp4"
                    if Path(dest_rel).suffix.upper() == ".MP4"
                    else "json"
                )
            verified = False
            try:
                verified = dest_file.is_file() and dest_file.stat().st_size == expected
            except OSError:
                verified = False
            if kind == "mp4" and dest_file.is_file() and item.get("embed_json"):
                if inventory.sidecar_for_mp4(dest_file) is None:
                    _ensure_dest_sidecar(dest_file, item.get("embed_json"))
                if inventory.sidecar_for_mp4(dest_file) is None:
                    raise RuntimeError(f"Verify failed: JSON sidecar missing for {dest_file.name}")
            if not verified:
                raise RuntimeError(f"Verify failed: {item['rel']} missing or wrong size under {dest}")
            copied = bool(item.get("copied"))
            already = bool(item.get("already_in_batch"))
            manifest.append(
                {
                    "source": str(item.get("source") or ""),
                    "rel": item.get("rel"),
                    "dest_rel": dest_rel,
                    "dest": str(dest_file),
                    "size": int(item.get("size") or 0),
                    "dest_size": expected,
                    "kind": kind,
                    "verified": True,
                    "wipe": copied and not already,
                    "already_in_batch": already,
                }
            )
        files_verified = len(manifest)
        files_already = sum(1 for r in manifest if r.get("already_in_batch"))
        files_copied = sum(1 for r in manifest if r.get("wipe"))
        if files_already:
            verify_msg = (
                f"{files_copied} / {len(files)} new files copied · "
                f"{files_already} already in this batch"
            )
        else:
            verify_msg = f"{files_verified} / {len(files)} files verified"
        _update_card(
            card_id,
            status="verifying",
            message=verify_msg,
            dest=str(dest),
            files_verified=files_verified,
            files_total=len(files),
            files_done=files_verified,
            files_already_in_batch=files_already,
            files_copied=files_copied,
        )
        _log_line(f"{card_id}: {verify_msg}", kind="ok")

        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")

        wipe_sources = [
            str(row["source"]) for row in manifest if row.get("wipe") and row.get("source")
        ]
        if wipe_sources:
            eject.assert_wipe_allowed(card_root, manifest)
        prog["status"] = "complete"
        progress.save_progress(card_root, prog)
        _update_card(
            card_id,
                status="wiping",
                message=f"{verify_msg} — wiping verified files on card…",
            dest=str(dest),
                files_verified=files_verified,
                files_total=len(files),
                files_already_in_batch=files_already,
                files_copied=files_copied,
            speed_mbps=0,
            eta_seconds=0,
            bytes_done=total_bytes,
        )
            eject.wipe_verified_sources(card_root, wipe_sources)
        else:
            prog["status"] = "complete"
            progress.save_progress(card_root, prog)
            _log_line(
                f"{card_id}: nothing new copied — SD card not wiped",
                kind="ok",
            )

        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")

        leftover_mp4s = inventory.list_card_mp4_paths(card_root)
        if not leftover_mp4s:
        try:
            _update_card(card_id, status="ejecting", message="Ejecting card…")
            eject.eject_volume(card_root)
        except Exception as eject_exc:  # noqa: BLE001
            _log_line(f"{card_id}: eject warning — {eject_exc}", kind="error")
        else:
            _log_line(
                f"{card_id}: {len(leftover_mp4s)} MP4(s) remain on the card — not ejecting",
                kind="ok",
            )

        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")

        port = reader_slot or ""
        label = reader_label or (f"Reader {port}" if port else "card reader")
        leftover_n = len(leftover_mp4s)
        if files_copied == 0 and files_already:
            done_msg = (
                f"0 / {len(files)} new files — {files_already} already in this batch — "
                "SD card not wiped"
            )
        elif leftover_n:
            done_msg = (
                f"{files_copied} / {len(files)} new files copied · "
                f"{files_already} already in this batch — "
                f"{leftover_n} file(s) left on SD (not wiped)"
            )
        elif port:
        done_msg = (
            f"Offloading of {label} has completed, insert a new card in port {port}"
        )
        else:
            done_msg = "Ready — card ejected"
        _update_card(
            card_id,
            status="completed",
            message=done_msg,
            speed_mbps=0,
            eta_seconds=0,
            bytes_done=total_bytes,
            files_verified=files_verified,
            files_total=len(files),
            files_already_in_batch=files_already,
            files_copied=files_copied,
            reader_slot=port,
            reader_label=label,
        )
        if port:
            push_notice(done_msg, kind="ok")
        _log_line(f"{card_id}: complete → {dest}", kind="ok")
    except CopyCancelled as exc:
        _clear_cancel_requested(card_id)
        _update_card(
            card_id,
            status="cancelled",
            message=(
                "Cancelled / card removed — files already on SSD are kept; card was not wiped. "
                "Re-insert and click Retry to resume (session stays armed)."
            ),
            dest=str(dest),
            speed_mbps=0.0,
            eta_seconds=None,
        )
        _log_line(f"{card_id}: {exc}", kind="ok")
    except Exception as exc:  # noqa: BLE001
        _clear_cancel_requested(card_id)
        _update_card(
            card_id,
            status="error",
            message=f"{exc} — click Retry to resume (files already on SSD are skipped)",
            dest=str(dest),
        )
        _log_line(f"{card_id}: error — {exc}", kind="error")
    finally:
        with _lock:
            _copy_threads.pop(card_id, None)
        _pump_waiting_queue()


def log_message(message: str, *, kind: str = "info") -> None:
    _log_line(message, kind=kind)


def bind_batch_context(
    *,
    batch: str,
    ssd1: str = "",
    ssd2: str = "",
    s3_uri: str = "",
) -> None:
    """Remember batch/SSD/S3 without starting the SD watcher (for AWS-only uploads)."""
    batch = batch.strip()
    if not batch:
        raise ValueError("Batch name is required")
    ssd1_path = str(Path(ssd1).resolve()) if ssd1 else ""
    ssd2_path = str(Path(ssd2).resolve()) if ssd2 else ""
    with _lock:
        _session["batch"] = batch
        if ssd1_path:
            _session["ssd1"] = ssd1_path
        if ssd2_path:
            _session["ssd2"] = ssd2_path
        if s3_uri.strip():
            _session["s3_uri"] = s3_uri.strip()
    save_config(
        {
            "last_batch": batch,
            "ssd1": _session.get("ssd1") or ssd1_path,
            "ssd2": _session.get("ssd2") or ssd2_path,
            "s3_uri": _session.get("s3_uri") or s3_uri.strip(),
        }
    )


def upload_batch_now(*, external_window: bool = True) -> dict:
    cfg = load_config()
    with _lock:
        batch = _session.get("batch") or cfg.get("last_batch") or ""
        ssd1 = _session.get("ssd1") or cfg.get("ssd1") or ""
        ssd2 = _session.get("ssd2") or cfg.get("ssd2") or ""
        s3_uri = _session.get("s3_uri") or cfg.get("s3_uri") or ""
    if not batch:
        raise ValueError("No batch selected — pick an existing batch or create a new one")
    if not s3_uri:
        raise ValueError("Set S3 URI first")
    if not ssd1 and not ssd2:
        raise ValueError("Pick SSD 1 / SSD 2 so we know where the batch lives")
    job = aws_upload.start_batch_upload(
        s3_uri=s3_uri,
        batch_name=batch,
        ssd1=ssd1,
        ssd2=ssd2,
        card_id=None,
        show_console=external_window,
        auto_delete=True,
    )
    _log_line(
        f"AWS upload started for batch {batch} → {job.get('dest')} "
        "(after verify, that batch folder is deleted on the SSD)"
    )
    return job

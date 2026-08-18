"""Session engine: watch SD cards, copy in parallel, optional AWS enqueue."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import aws_upload, eject, embed_meta, inventory, pairing, progress, space
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
}
_cards: dict[str, dict] = {}  # card_id -> job state
_copy_threads: dict[str, threading.Thread] = {}
_batch_dest_locks: dict[str, threading.Lock] = {}
_cancel_requested: set[str] = set()
_waiting_queue: list[dict] = []  # queued starts when at max parallel
_watcher_started = False
_log: list[dict] = []
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
    aws_upload.restore_jobs_from_disk()
    if not SNAPSHOT_FILE.exists():
        return
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
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
    if _session.get("active"):
        # Do NOT auto-resume the SD watcher on startup — scanning drives during
        # boot was freezing the web UI on hung card readers. User clicks Start.
        _session["active"] = False
        _log_line(
            "Previous session was active — click Start SD → SSD to resume watching",
            kind="ok",
        )


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
        }
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

    with _lock:
        _session.update(
            {
                "active": True,
                "batch": batch,
                "mode": mode,
                "ssd1": ssd1_path,
                "ssd2": ssd2_path,
                "s3_uri": s3_uri.strip(),
                "started_at": time.time(),
            }
        )
        # Allow previously cancelled cards to be picked up again on Start.
        for cid, card in list(_cards.items()):
            if card.get("status") == "cancelled":
                _cards.pop(cid, None)
                _cancel_requested.discard(cid)
    save_config(
        {
            "last_batch": batch,
            "mode": mode,
            "ssd1": ssd1_path,
            "ssd2": ssd2_path,
            "s3_uri": s3_uri.strip(),
        }
    )
    # Ensure batch folders exist on available SSDs
    for ssd in (ssd1_path, ssd2_path):
        if ssd:
            space.batch_root(ssd, batch).mkdir(parents=True, exist_ok=True)

    _ensure_watcher()
    _log_line(
        f"Session started: {batch} ({mode}) — hotplug armed "
        "(insert/remove SDs anytime; new cards auto SD→SSD→AWS)"
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
                        "message": f"Already on SSD: {dest_hint}",
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

    try:
        ssd_path, _ = space.pick_ssd_for_bytes(ssd1=ssd1, ssd2=ssd2, needed_bytes=total)
        dest = space.batch_root(ssd_path, batch)
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
        "batch": batch,
        "card_id": card_id,
        "dest": str(dest),
        "files": {},
        "status": "in_progress",
        "bytes_total": total,
    }
    prog.update(
        {
            "batch": batch,
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
        "batch": batch,
        "mode": mode,
        "s3_uri": s3_uri,
        "files": files,
        "dest": dest,
        "prog": prog,
        "ssd_path": ssd_path,
        "total": total,
        "volume_serial": volume_serial,
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
            "bytes_done": 0,
            "bytes_total": total,
            "speed_mbps": 0.0,
            "eta_seconds": None,
            "files_total": len(files),
            "files_done": 0,
            "started_at": time.time(),
        }

    _log_line(f"{card_id}: starting copy → {dest} ({len(files)} files, {total} bytes)")
    thread = threading.Thread(
        target=_copy_card_worker,
        args=(card_root, card_id, batch, mode, s3_uri, files, dest, prog),
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


def _is_sidecar_rel(rel: str) -> bool:
    lower = rel.lower()
    return lower.endswith(".segments.json") or lower.endswith(".json")


def _sidecar_stem(rel: str) -> str:
    lower = rel.lower()
    if lower.endswith(".segments.json"):
        return rel[: -len(".segments.json")]
    if lower.endswith(".json"):
        return rel[: -len(Path(rel).suffix)]
    return Path(rel).stem


def _dest_batch_lock(dest: Path) -> threading.Lock:
    key = str(dest.resolve()).lower()
    with _lock:
        if key not in _batch_dest_locks:
            _batch_dest_locks[key] = threading.Lock()
        return _batch_dest_locks[key]


def _resolve_dest_names(files: list[dict], dest: Path, prog: dict, card_id: str) -> None:
    """Assign collision-safe destination names into the flat batch folder.

    GoPro numbering repeats across cards (every card has a GX010001.MP4), so
    when another card already parked a *different* video under the same name,
    the incoming pair is renamed to ``<stem>__<CARDID><ext>``. When the batch
    already holds the same video (size + sidecar identity), the existing name
    is reused so re-offloads do not create duplicates.
    """
    entries = prog.get("files") or {}
    stem_map: dict[str, str] = {}  # source MP4 stem -> final stem

    def assign_one(item: dict, *, sidecar_pass: bool) -> None:
        rel = item["rel"]
        if item.get("task"):
            item["dest_rel"] = rel  # legacy task folders keep their layout
            return

        recorded = (entries.get(rel) or {}).get("dest_rel")
        is_sidecar = _is_sidecar_rel(rel)
        if is_sidecar != sidecar_pass:
            return

        base = _sidecar_stem(rel) if is_sidecar else Path(rel).stem
        suffix = Path(rel).suffix
        if rel.lower().endswith(".segments.json"):
            suffix = ".segments.json"

        if is_sidecar:
            final_base = stem_map.get(base, base)
            if recorded:
                item["dest_rel"] = recorded
            elif suffix.lower() == ".segments.json":
                item["dest_rel"] = f"{final_base}.segments.json"
            else:
                item["dest_rel"] = f"{final_base}{suffix}"
            return

        if recorded:
            item["dest_rel"] = recorded
            stem_map[base] = Path(recorded).stem
            return

        name = Path(rel).name
        card_size = int(item.get("size") or 0)
        sidecar_payload = (
            pairing.load_sidecar(item["embed_json"]) if item.get("embed_json") else None
        )
        existing = pairing.find_existing_dest_name(
            dest, name, card_mp4_size=card_size, sidecar=sidecar_payload
        )
        if existing:
            item["dest_rel"] = existing
            stem_map[base] = Path(existing).stem
            return

        if (dest / name).exists():
            final_base = f"{base}__{card_id.upper()}"
            item["dest_rel"] = f"{final_base}{Path(rel).suffix}"
            stem_map[base] = final_base
        else:
            item["dest_rel"] = name
            stem_map[base] = base

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


def _copy_card_worker(
    card_root: Path,
    card_id: str,
    batch: str,
    mode: str,
    s3_uri: str,
    files: list[dict],
    dest: Path,
    prog: dict,
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
        speed_mbps=0.0,
        eta_seconds=None,
    )
    started = time.time()
    done_bytes = 0
    files_done = 0
    task_names = sorted({f["task"] for f in files if f.get("task")})
    root_rels = sorted(f["rel"] for f in files if not f.get("task"))
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
            dest_rel = item.get("dest_rel") or rel
            dest_file = dest / dest_rel
            if dest_rel != rel and not (prog.get("files") or {}).get(rel):
                if Path(rel).name == dest_rel:
                    _log_line(
                        f"{card_id}: {rel} already in batch — reusing {dest_rel} (no duplicate)"
                    )
                else:
                    _log_line(
                        f"{card_id}: {rel} already in batch from another card — saving as {dest_rel}"
                    )
            if progress.is_file_done(prog, rel, size, dest_file):
                try:
                    item["dest_size"] = dest_file.stat().st_size
                except OSError:
                    item["dest_size"] = size
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

            # Embed the GoPro Cleaner segments JSON inside the SSD copy so the
            # MP4 itself carries task names + timestamps (sidecar still copied
            # alongside). Card original is never modified.
            dest_size = size
            sidecar_path = item.get("embed_json") or ""
            if sidecar_path:
                if _is_cancel_requested(card_id):
                    raise CopyCancelled(f"{card_id}: cancelled by operator")
                try:
                    payload = json.loads(
                        Path(sidecar_path).read_text(encoding="utf-8")
                    )
                    mismatches = pairing.validate_sidecar_for_mp4(
                        src, Path(sidecar_path), payload
                    )
                    if mismatches:
                        _log_line(
                            f"{card_id}: {rel} metadata mismatch — "
                            f"{'; '.join(mismatches)} (sidecar copied; embed skipped)",
                            kind="error",
                        )
                    else:
                        missing = _missing_metadata(payload)
                        if missing:
                            _log_line(
                                f"{card_id}: {rel} sidecar is missing "
                                f"{', '.join(missing)} — re-check in GoPro Cleaner",
                                kind="error",
                            )
                        embed_meta.embed_segments_json(dest_file, payload)
                        dest_size = dest_file.stat().st_size
                        _log_line(f"{card_id}: embedded segments into {dest_rel}")
                except CopyCancelled:
                    raise
                except Exception as embed_exc:  # noqa: BLE001
                    _log_line(
                        f"{card_id}: could not embed segments into {rel} — {embed_exc}",
                        kind="error",
                    )
            item["dest_size"] = dest_size

            progress.mark_file_done(
                card_root, prog, rel, size, dest_size=dest_size, dest_rel=dest_rel
            )
            done_bytes += size
            files_done += 1
            _publish(0, message=f"Copied {rel}", force=True)

        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")

        if not saw_disk_write and files:
            raise RuntimeError(
                f"No files were written under {dest} — check SSD path and card folders"
            )

        _update_card(card_id, status="verifying", message=f"Verifying {dest}…", dest=str(dest))
        for item in files:
            if _is_cancel_requested(card_id):
                raise CopyCancelled(f"{card_id}: cancelled by operator")
            dest_file = dest / (item.get("dest_rel") or item["rel"])
            expected = int(item.get("dest_size") or item["size"])
            if not dest_file.exists() or dest_file.stat().st_size != expected:
                raise RuntimeError(f"Verify failed: {item['rel']} missing under {dest}")

        prog["status"] = "complete"
        progress.save_progress(card_root, prog)

        # Mark DONE before wipe/eject so a mid-wipe watcher pass cannot flip us to ERROR.
        _update_card(
            card_id,
            status="completed",
            message="Copy verified — wiping & ejecting…",
            dest=str(dest),
            speed_mbps=0,
            eta_seconds=0,
            bytes_done=total_bytes,
        )

        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")

        try:
            _update_card(card_id, status="wiping", message="Wiping transferred files on card…")
            # Keep completed semantics if wipe/eject races the watcher.
            eject.wipe_transferred_tasks(card_root, task_names, root_rels)
        except Exception as wipe_exc:  # noqa: BLE001
            _log_line(f"{card_id}: wipe warning — {wipe_exc}", kind="error")

        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")

        try:
            _update_card(card_id, status="ejecting", message="Ejecting card…")
            eject.eject_volume(card_root)
        except Exception as eject_exc:  # noqa: BLE001
            _log_line(f"{card_id}: eject warning — {eject_exc}", kind="error")

        if _is_cancel_requested(card_id):
            raise CopyCancelled(f"{card_id}: cancelled by operator")

        if mode == "ssd_and_aws" and s3_uri:
            _update_card(card_id, status="uploading", message="Syncing batch folder to AWS…")
            with _lock:
                ssd1 = _session.get("ssd1") or ""
                ssd2 = _session.get("ssd2") or ""
            try:
                # Flat batch layout: always sync whole Batches/<batch>/ (not a
                # per-card subfolder). card_id is only a trigger label.
                job = aws_upload.start_batch_upload(
                    s3_uri=s3_uri,
                    batch_name=batch,
                    ssd1=ssd1,
                    ssd2=ssd2,
                    card_id=card_id,
                    show_console=True,
                )
                coalesced = bool(job.get("pending_resync")) or "resync" in str(job.get("message") or "").lower()
                _update_card(
                    card_id,
                    status="completed",
                    message=(
                        f"Ready — batch AWS upload already running; resync queued ({job.get('id')})"
                        if coalesced
                        else f"Ready — batch AWS upload live in UI ({job.get('id')})"
                    ),
                    speed_mbps=0,
                    eta_seconds=0,
                    bytes_done=total_bytes,
                )
            except Exception as exc:  # noqa: BLE001
                _update_card(
                    card_id,
                    status="completed",
                    message=f"SSD copy done; AWS failed to start: {exc}",
                )
                _log_line(f"{card_id}: AWS enqueue failed: {exc}", kind="error")
        else:
            _update_card(
                card_id,
                status="completed",
                message="Ready — card ejected (SSD only)",
                speed_mbps=0,
                eta_seconds=0,
                bytes_done=total_bytes,
            )

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
    )
    _log_line(f"AWS upload started for batch {batch} → {job.get('dest')} (live progress in UI)")
    return job

"""Session engine: watch SD cards, copy in parallel, optional AWS enqueue."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import aws_upload, eject, inventory, progress, space
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
_watcher_started = False
_log: list[dict] = []
SNAPSHOT_FILE = STATE_DIR / "ui_snapshot.json"
_last_snapshot_at = 0.0
ACTIVE_COPY_STATUSES = {
    "queued",
    "copying",
    "verifying",
    "wiping",
    "ejecting",
    "uploading",
}


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
        _ensure_watcher()
        _log_line("Restored session — watching for SD cards again", kind="ok")


def get_status() -> dict:
    with _lock:
        cards = [dict(c) for c in _cards.values()]
        status = {
            "session": dict(_session),
            "cards": sorted(cards, key=lambda c: c.get("started_at") or 0, reverse=True),
            "log": list(_log[-80:]),
            "aws_jobs": aws_upload.list_jobs()[:20],
            # volumes are heavy on Windows — UI loads them via /api/volumes
        }
    _save_snapshot(force=False)
    return status


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
    _log_line(f"Session started: {batch} ({mode})")
    # Immediately scan once
    _scan_for_cards()
    return get_status()


def stop_session() -> dict:
    with _lock:
        _session["active"] = False
    _log_line("Session stopped (watcher idle; in-flight copies continue)")
    return get_status()


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
        except Exception as exc:  # noqa: BLE001
            _log_line(f"Watcher error: {exc}", kind="error")
        time.sleep(2.0)


def _scan_for_cards() -> None:
    with _lock:
        ssd1 = _session.get("ssd1") or ""
        ssd2 = _session.get("ssd2") or ""
        exclude = {p for p in (ssd1, ssd2) if p}
        batch = _session.get("batch") or ""
        mode = _session.get("mode") or "ssd_only"
        s3_uri = _session.get("s3_uri") or ""

    cards = find_card_volumes(exclude_paths=exclude)
    for vol in cards:
        card_id = _resolve_card_id(vol)
        if not card_id:
            _log_line(
                f"Skipping volume {vol.get('path')}: no C#### card id "
                "(rename volume label to C1234 or add a C1234 folder)",
                kind="error",
            )
            continue
        card_root = Path(vol["path"])
        with _lock:
            existing = _cards.get(card_id)
            thread = _copy_threads.get(card_id)
            thread_alive = bool(thread and thread.is_alive())
            if existing and existing.get("status") in ACTIVE_COPY_STATUSES and thread_alive:
                # Real in-flight worker — do not restart
                if existing.get("mount") == str(card_root):
                    continue
            if existing and existing.get("status") == "completed" and thread_alive:
                continue
            # Stale UI "copying" with dead thread must be restarted
            if existing and existing.get("status") in ACTIVE_COPY_STATUSES and not thread_alive:
                existing["status"] = "interrupted"
                existing["message"] = "Copy worker stopped — restarting transfer…"
                _log_line(f"{card_id}: stale {existing.get('status')} state — restarting", kind="error")

        prog = progress.load_progress(card_root)
        if prog and prog.get("status") == "complete" and prog.get("batch") == batch:
            dest_hint = Path(str(prog.get("dest") or ""))
            if dest_hint.is_dir() and progress.dest_looks_complete(prog, dest_hint):
                with _lock:
                    _cards[card_id] = {
                        "card_id": card_id,
                        "mount": str(card_root),
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

        _start_card_job(card_root, card_id, batch, mode, ssd1, ssd2, s3_uri, prog)


def _resolve_card_id(vol: dict) -> str:
    raw = (vol.get("card_id") or "").strip().upper()
    if raw:
        return raw
    path = Path(str(vol.get("path") or ""))
    # Windows drive root has empty .name — never use that as card id
    name = path.name.strip().upper()
    if name and name not in {path.anchor.strip("\\/").upper(), ""}:
        if len(name) >= 2:
            return name
    # Last resort: drive letter
    anchor = path.anchor.replace("\\", "").replace(":", "").replace("/", "").upper()
    if anchor:
        return f"DRIVE-{anchor}"
    return ""


def _start_card_job(
    card_root: Path,
    card_id: str,
    batch: str,
    mode: str,
    ssd1: str,
    ssd2: str,
    s3_uri: str,
    existing_progress: dict | None,
) -> None:
    files = inventory.list_transfer_files(card_root)
    total = inventory.total_bytes(files)
    if not files:
        _log_line(f"{card_id}: no task MP4 folders under DCIM/…GOPRO", kind="error")
        with _lock:
            _cards[card_id] = {
                "card_id": card_id,
                "mount": str(card_root),
                "status": "error",
                "message": "No labeled task folders with MP4s found",
                "bytes_done": 0,
                "bytes_total": 0,
                "speed_mbps": 0,
                "eta_seconds": None,
                "started_at": time.time(),
            }
        return

    try:
        ssd_path, _ = space.pick_ssd_for_bytes(ssd1=ssd1, ssd2=ssd2, needed_bytes=total)
        dest = space.card_dest(ssd_path, batch, card_id)
        dest.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        _log_line(f"{card_id}: {exc}", kind="error")
        with _lock:
            _cards[card_id] = {
                "card_id": card_id,
                "mount": str(card_root),
                "status": "error",
                "message": str(exc),
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
    # Always re-bind dest to the SSD we just picked (avoid stale progress path)
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

    with _lock:
        _cards[card_id] = {
            "card_id": card_id,
            "mount": str(card_root),
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
    task_names = sorted({f["task"] for f in files})
    last_ui = 0.0
    last_live = 0
    last_speed_at = started
    saw_disk_write = False

    def _publish(current_file_bytes: int = 0, *, message: str | None = None, force: bool = False) -> None:
        nonlocal last_ui, last_live, last_speed_at
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
        for item in files:
            rel = item["rel"]
            src = Path(item["source"])
            size = int(item["size"])
            dest_file = dest / rel
            if progress.is_file_done(prog, rel, size, dest_file):
                done_bytes += size
                files_done += 1
                saw_disk_write = True
                _publish(0, message=f"Skipped (done): {rel}", force=True)
                continue

            if not src.is_file():
                raise RuntimeError(f"Source missing on card: {src}")

            _publish(0, message=f"Copying {rel} → {dest_file}", force=True)

            def on_progress(written: int, _rel: str = rel, _dest_file: Path = dest_file) -> None:
                nonlocal saw_disk_write
                partial = _dest_file.with_suffix(_dest_file.suffix + ".partial")
                if written > 0 and (partial.exists() or _dest_file.exists()):
                    saw_disk_write = True
                _publish(written, message=f"Copying {_rel}…")

            copy_file(src, dest_file, on_progress=on_progress)
            if not dest_file.is_file() or dest_file.stat().st_size != size:
                raise RuntimeError(
                    f"Copy did not land on SSD: expected {dest_file} ({size} bytes)"
                )
            saw_disk_write = True
            progress.mark_file_done(card_root, prog, rel, size)
            done_bytes += size
            files_done += 1
            _publish(0, message=f"Copied {rel}", force=True)

        if not saw_disk_write and files:
            raise RuntimeError(
                f"No files were written under {dest} — check SSD path and card folders"
            )

        _update_card(card_id, status="verifying", message=f"Verifying {dest}…", dest=str(dest))
        for item in files:
            dest_file = dest / item["rel"]
            if not dest_file.exists() or dest_file.stat().st_size != int(item["size"]):
                raise RuntimeError(f"Verify failed: {item['rel']} missing under {dest}")

        prog["status"] = "complete"
        progress.save_progress(card_root, prog)

        _update_card(card_id, status="wiping", message="Wiping transferred folders on card…")
        eject.wipe_transferred_tasks(card_root, task_names)

        _update_card(card_id, status="ejecting", message="Ejecting card…")
        eject.eject_volume(card_root)

        if mode == "ssd_and_aws" and s3_uri:
            _update_card(card_id, status="uploading", message="Queued for AWS upload…")
            with _lock:
                ssd1 = _session.get("ssd1") or ""
                ssd2 = _session.get("ssd2") or ""
            try:
                job = aws_upload.start_batch_upload(
                    s3_uri=s3_uri,
                    batch_name=batch,
                    ssd1=ssd1,
                    ssd2=ssd2,
                    card_id=card_id,
                    show_console=True,
                )
                _update_card(
                    card_id,
                    status="completed",
                    message=f"Ready — AWS upload live in UI ({job.get('id')})",
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
    except Exception as exc:  # noqa: BLE001
        _update_card(card_id, status="error", message=str(exc), dest=str(dest))
        _log_line(f"{card_id}: error — {exc}", kind="error")
    finally:
        with _lock:
            _copy_threads.pop(card_id, None)


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

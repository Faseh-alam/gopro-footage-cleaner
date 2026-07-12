"""Session engine: watch SD cards, copy in parallel, optional AWS enqueue."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from . import aws_upload, eject, inventory, progress, space
from .config import load_config, save_config
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
_watcher_started = False
_log: list[dict] = []


def _log_line(message: str, *, kind: str = "info") -> None:
    with _lock:
        _log.append({"t": time.time(), "kind": kind, "message": message})
        if len(_log) > 300:
            del _log[:-300]


def get_status() -> dict:
    with _lock:
        cards = [dict(c) for c in _cards.values()]
        return {
            "session": dict(_session),
            "cards": sorted(cards, key=lambda c: c.get("started_at") or 0, reverse=True),
            "log": list(_log[-80:]),
            "aws_jobs": aws_upload.list_jobs()[:20],
            "volumes": list_volumes(),
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
        card_id = (vol.get("card_id") or Path(vol["path"]).name).upper()
        card_root = Path(vol["path"])
        with _lock:
            existing = _cards.get(card_id)
            if existing and existing.get("status") in {
                "queued",
                "copying",
                "verifying",
                "wiping",
                "ejecting",
                "uploading",
                "completed",
            }:
                # Allow resume if previously error / interrupted and card remounted
                if existing.get("status") == "completed":
                    continue
                if existing.get("status") != "error" and existing.get("mount") == str(card_root):
                    continue

        prog = progress.load_progress(card_root)
        if prog and prog.get("status") == "complete" and prog.get("batch") == batch:
            with _lock:
                _cards[card_id] = {
                    "card_id": card_id,
                    "mount": str(card_root),
                    "status": "completed",
                    "message": "Already completed earlier (progress file)",
                    "bytes_done": prog.get("bytes_total") or 0,
                    "bytes_total": prog.get("bytes_total") or 0,
                    "speed_mbps": 0,
                    "eta_seconds": 0,
                    "started_at": time.time(),
                }
            continue

        _start_card_job(card_root, card_id, batch, mode, ssd1, ssd2, s3_uri, prog)


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

    _log_line(f"{card_id}: starting copy to {dest}")
    thread = threading.Thread(
        target=_copy_card_worker,
        args=(card_root, card_id, batch, mode, s3_uri, files, dest, prog),
        daemon=True,
        name=f"copy-{card_id}",
    )
    thread.start()


def _update_card(card_id: str, **kwargs) -> None:
    with _lock:
        if card_id in _cards:
            _cards[card_id].update(kwargs)


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
    _update_card(card_id, status="copying", message="Copying…", bytes_done=0, files_done=0)
    started = time.time()
    done_bytes = 0
    files_done = 0
    task_names = sorted({f["task"] for f in files})
    last_ui = 0.0

    def _publish(current_file_bytes: int = 0, *, message: str | None = None) -> None:
        nonlocal last_ui
        now = time.time()
        # Throttle UI updates to ~4/sec while streaming a large file
        if message is None and now - last_ui < 0.25:
            return
        last_ui = now
        live = done_bytes + max(0, current_file_bytes)
        elapsed = max(0.1, now - started)
        speed = (live / (1024 * 1024)) / elapsed if live > 0 else 0.0
        remaining = max(0, total_bytes - live)
        eta = int(remaining / (speed * 1024 * 1024)) if speed > 0 else None
        payload = {
            "status": "copying",
            "bytes_done": live,
            "files_done": files_done,
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
                _publish(0, message=f"Skipped (done): {rel}")
                continue

            _publish(0, message=f"Copying {rel}…")

            def on_progress(written: int, _rel: str = rel) -> None:
                _publish(written, message=f"Copying {_rel}…")

            copy_file(src, dest_file, on_progress=on_progress)
            progress.mark_file_done(card_root, prog, rel, size)
            done_bytes += size
            files_done += 1
            _publish(0, message=f"Copied {rel}")

        _update_card(card_id, status="verifying", message="Verifying…")
        for item in files:
            dest_file = dest / item["rel"]
            if not dest_file.exists() or dest_file.stat().st_size != int(item["size"]):
                raise RuntimeError(f"Verify failed: {item['rel']}")

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
                    external_window=True,
                )
                _update_card(
                    card_id,
                    status="completed",
                    message=f"Ready — AWS job {job.get('id')} started",
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
        _update_card(card_id, status="error", message=str(exc))
        _log_line(f"{card_id}: error — {exc}", kind="error")


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
        external_window=external_window,
    )
    _log_line(f"AWS upload window opened for batch {batch} → {job.get('dest')}")
    return job

"""v2 addition: trigger the AWS batch sync when an SSD is actually full, not per-card.

This is a separate, opt-in mode (``ssd_and_aws_full_v2`` in the mode dropdown /
``start_session``) — the original ``ssd_and_aws`` per-card-trigger code path in
``engine.py`` is untouched. Engine calls into this module only when the new
mode is explicitly selected; dependencies (update_card/log_line/skip_note) are
passed in rather than imported from engine, so there's no circular import and
this file has zero effect unless the new mode is chosen.

"Full" is free-space based (config: ``ssd_full_reserve_gb``, default 10 GB),
not a percentage — a 500 GB and a 4 TB SSD both trigger at the same real
"can't safely fit another card" point.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from . import aws_upload, slack_alert
from .aws_upload import _compare_local_s3_sizes, batch_s3_prefix, list_local_batch_roots
from .config import load_config
from .detect import volume_free_bytes


def ssd_full_reserve_bytes() -> int:
    try:
        gb = max(0.5, float(load_config().get("ssd_full_reserve_gb") or 10))
    except (TypeError, ValueError):
        gb = 10.0
    return int(gb * 1024**3)


def _free_gb(path: str | Path) -> float | None:
    try:
        return volume_free_bytes(path) / 1024**3
    except OSError:
        return None


def _ssd_is_full(dest: Path) -> float | None:
    """Free GB remaining on dest's volume if below the "full" reserve, else None."""
    try:
        free_bytes = volume_free_bytes(dest)
    except OSError:
        return None
    if free_bytes >= ssd_full_reserve_bytes():
        return None
    return free_bytes / 1024**3


def maybe_trigger_upload(
    *,
    card_id: str,
    dest: Path,
    batch: str,
    s3_uri: str,
    total_bytes: int,
    ssd1: str,
    ssd2: str,
    update_card: Callable[..., None],
    log_line: Callable[..., None],
    skip_note: Callable[[str], str],
    force: bool = False,
) -> None:
    """Called after a card finishes copying, in ``ssd_and_aws_full_v2`` mode only.

    Below the reserve threshold: just marks the card ready, no sync starts.
    At/below it — or when ``force`` is set (operator's one-shot "this is my
    last card, sync now" override, see engine.set_force_sync_next_card) —
    fires the same ``aws_upload.start_batch_upload`` the original per-card
    mode uses, so CMD-window progress / retry / verify all behave
    identically — only the trigger condition is new.
    """
    full_free_gb = _ssd_is_full(dest)
    if full_free_gb is None and not force:
        free_gb = _free_gb(dest)
        reserve_gb = ssd_full_reserve_bytes() / 1024**3
        message = (
            f"Ready — card ejected; SSD has {free_gb:.1f} GB free "
            f"(uploads once it drops below {reserve_gb:.0f} GB)"
            if free_gb is not None
            else "Ready — card ejected (waiting for SSD to fill before AWS sync)"
        )
        update_card(
            card_id,
            status="completed",
            message=message + skip_note(card_id),
            speed_mbps=0,
            eta_seconds=0,
            bytes_done=total_bytes,
        )
        return

    reason = (
        f"SSD full ({full_free_gb:.1f} GB free left)"
        if full_free_gb is not None
        else "forced by operator (last card)"
    )
    update_card(
        card_id,
        status="uploading",
        message=f"{reason} — syncing batch to AWS…",
    )
    try:
        job = aws_upload.start_batch_upload(
            s3_uri=s3_uri,
            batch_name=batch,
            ssd1=ssd1,
            ssd2=ssd2,
            card_id=card_id,
            show_console=True,
        )
        coalesced = bool(job.get("pending_resync")) or "resync" in str(job.get("message") or "").lower()
        update_card(
            card_id,
            status="completed",
            message=(
                (
                    f"Ready — batch AWS upload already running; resync queued ({job.get('id')})"
                    if coalesced
                    else f"Ready — {reason}, batch AWS upload live in UI ({job.get('id')})"
                )
                + skip_note(card_id)
            ),
            speed_mbps=0,
            eta_seconds=0,
            bytes_done=total_bytes,
        )
    except Exception as exc:  # noqa: BLE001
        update_card(
            card_id,
            status="completed",
            message=f"SSD copy done; AWS failed to start: {exc}",
        )
        log_line(f"{card_id}: AWS enqueue failed: {exc}", kind="error")
        slack_alert.send_alert(
            f":rotating_light: Offloader — AWS upload failed to *start* for batch "
            f"`{batch}` ({reason} after card {card_id}): {exc}"
        )


def verify_batch_v2(*, batch: str, ssd1: str, ssd2: str, s3_uri: str) -> dict:
    """Fresh, whole-batch verify across BOTH currently configured SSDs.

    Unlike ``aws_upload.verify_job_sizes`` — scoped to one job's stored
    ``sources`` snapshot, frozen at whatever the batch's footprint was when
    that job happened to be created — this recomputes the batch's current
    local roots from live config on every call, so it can't show "verified"
    for only part of a batch that later grew onto a second SSD. That gap is
    exactly what produced a green VERIFIED job sitting next to red SIZE
    MISMATCH jobs for the same batch in the UI.
    """
    batch = batch.strip()
    if not batch:
        raise ValueError("Batch name is required")
    if not s3_uri.strip():
        raise ValueError("S3 URI is required")
    dest = batch_s3_prefix(s3_uri, batch)
    roots = list_local_batch_roots(ssd1, ssd2, batch)
    if not roots:
        raise RuntimeError(f"No local batch folder found for {batch} on the selected SSDs")
    result = _compare_local_s3_sizes(roots, dest)
    result["batch"] = batch
    result["dest"] = dest
    result["roots"] = [str(r) for r in roots]
    return result


def delete_local_batch_v2(
    *, batch: str, ssd1: str, ssd2: str, s3_uri: str, confirmed: bool
) -> dict:
    """Delete the batch's local folders on every currently configured SSD —
    only after a fresh whole-batch verify (recomputed here, never trusted
    from a stale per-job snapshot) confirms local matches S3 everywhere.
    """
    if not confirmed:
        raise ValueError("Deletion requires confirmed=true")
    check = verify_batch_v2(batch=batch, ssd1=ssd1, ssd2=ssd2, s3_uri=s3_uri)
    if not check.get("ok"):
        raise RuntimeError(
            "Refusing delete — local vs S3 do not match across the batch's current "
            f"SSDs (local={check.get('local_bytes')} s3={check.get('s3_bytes')} "
            f"delta={check.get('delta')})"
        )

    roots = list_local_batch_roots(ssd1, ssd2, batch)
    # Re-check right before delete in case anything changed since the verify above.
    recheck = _compare_local_s3_sizes(roots, batch_s3_prefix(s3_uri, batch))
    if not recheck.get("ok"):
        raise RuntimeError("Sizes no longer match — refusing to delete local files")

    deleted: list[str] = []
    errors: list[str] = []
    for root in roots:
        try:
            if root.is_dir():
                shutil.rmtree(root)
                deleted.append(str(root))
        except OSError as exc:
            errors.append(f"{root}: {exc}")

    if errors and not deleted:
        raise RuntimeError("; ".join(errors))
    return {"ok": True, "batch": batch, "deleted": deleted, "errors": errors}

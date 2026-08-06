"""Utility helpers for probing cards and building batch reports – no local state."""

from __future__ import annotations

import csv
import io
import datetime
from pathlib import Path
from typing import Any
import shutil

from .probe import probe_media

PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------
def _now_12h_time() -> str:
    return datetime.datetime.now().strftime("%I:%M %p").lstrip("0")


def _format_time(val: Any) -> str:
    if not val:
        return ""
    val_str = str(val).strip()
    if "AM" in val_str.upper() or "PM" in val_str.upper():
        return val_str
    try:
        dt = datetime.datetime.fromisoformat(val_str)
        return dt.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return val_str


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _format_duration(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "00:00:00"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _format_gb(gb: float) -> str:
    if gb < 0.01:
        return "0.00 GB"
    return f"{gb:.2f} GB"


# ----------------------------------------------------------------------
# Media probing
# ----------------------------------------------------------------------
def _probe_duration(video_path: Path) -> float | None:
    """Safely probe a media file for duration (seconds). Returns None on failure."""
    try:
        info = probe_media(video_path)
        return info.duration if info and info.duration else None
    except Exception:
        return None


def _bytes_to_gb(size_bytes: int) -> float:
    value = size_bytes / (1024.0 * 1024.0 * 1024.0)
    if value < 0.0005:
        return 0.0
    return round(value, 3)


# ----------------------------------------------------------------------
# Card statistics (directly from disk)
# ----------------------------------------------------------------------
def card_stats(card_path: str | Path) -> dict[str, Any]:
    root = Path(card_path).expanduser().resolve()

    # Total volume capacity
    try:
        total_bytes = shutil.disk_usage(str(root)).total
        card_capacity = _bytes_to_gb(total_bytes)
    except OSError:
        card_capacity = None

    mp4_files = list(root.rglob("*.mp4"))

    original_duration = 0.0
    for video in mp4_files:
        dur = _probe_duration(video)
        if dur is not None:
            original_duration += dur

    # Total used space on the card (all files)
    used_space_before_bytes = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                used_space_before_bytes += path.stat().st_size
            except OSError:
                continue

    used_space_after_bytes = used_space_before_bytes   # no trimming

    return {
        "total_mp4_videos": len(mp4_files),
        "original_duration": round(original_duration, 3),
        "final_duration": round(original_duration, 3),
        "card_capacity": card_capacity,
        "used_space_before_labeling_gb": _bytes_to_gb(used_space_before_bytes),
        "used_space_after_labeling_gb": _bytes_to_gb(used_space_after_bytes),
        "video_sizes": {v.resolve(): v.stat().st_size for v in mp4_files if v.is_file()},
    }


# ----------------------------------------------------------------------
# Batch report builders (unchanged – use as needed)
# ----------------------------------------------------------------------
def build_report_rows(batch_detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one row per card with batch-level and per-card metrics."""
    rows: list[dict[str, Any]] = []
    batch_name = str(batch_detail.get("batch_name") or "")
    factory = str(batch_detail.get("factory") or "")
    cards = batch_detail.get("cards") or []

    for card in cards:
        badge = str(card.get("card_badge") or "")
        device_type = str(card.get("device_type") or "")
        device_id = str(card.get("device_id") or "")
        status = str(card.get("status") or "")
        assets = card.get("assets") or []

        raw_seconds = 0.0
        missing_file = False
        complete = True

        for asset in assets:
            duration = _coerce_float(asset.get("duration"))
            if not duration:
                asset_path = asset.get("path")
                if asset_path:
                    probed = _probe_duration(Path(asset_path))
                    if probed is not None:
                        duration = probed
                    else:
                        complete = False
            raw_seconds += duration
            asset_path = asset.get("path")
            if asset_path and not Path(asset_path).is_file():
                missing_file = True
                complete = False

        row = {
            "batch_name": batch_name,
            "factory": factory,
            "card_badge": badge,
            "device_type": device_type,
            "device_id": device_id,
            "status": status,
            "video_count": len(assets),
            "raw_seconds": round(raw_seconds, 3),
            "raw_hours": round(raw_seconds / 3600.0, 4),
            "work_seconds": round(raw_seconds, 3),
            "work_hours": round(raw_seconds / 3600.0, 4),
            "garbage_seconds": 0.0,
            "garbage_hours": 0.0,
            "unreviewed_seconds": 0.0,
            "unreviewed_hours": 0.0,
            "complete": complete,
            "missing_file": missing_file,
            "segment_count": 0,
            "task_summary": "",
        }
        if card.get("bound_at"):
            row["bound_at"] = card.get("bound_at")
        if card.get("completed_at"):
            row["completed_at"] = card.get("completed_at")
        rows.append(row)

    return rows


def rows_to_csv(rows: list[dict[str, Any]], *, fieldnames: list[str] | None = None) -> str:
    if not rows:
        return ""
    ordered_fieldnames = fieldnames or [
        "batch_name",
        "factory",
        "card_badge",
        "device_type",
        "device_id",
        "status",
        "video_count",
        "raw_hours",
        "work_hours",
        "garbage_hours",
        "unreviewed_hours",
        "complete",
        "missing_file",
        "segment_count",
        "task_summary",
        "bound_at",
        "completed_at",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=ordered_fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in ordered_fieldnames})
    return buffer.getvalue()


def export_report_csv(batch_detail: dict[str, Any]) -> str:
    return rows_to_csv(build_report_rows(batch_detail))
"""Per-employee daily metrics: work hours, SD cards, footage processed."""

from __future__ import annotations

import datetime
from typing import Any, Optional

from . import supabase_db
from .card_identity import card_id_from_serial


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def ensure_daily_metrics(employee_id: str, work_date: Optional[str] = None) -> dict[str, Any]:
    day = work_date or supabase_db.today_date_str()
    client = supabase_db.get_supabase()
    existing = (
        client.table("employee_daily_metrics")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("work_date", day)
        .limit(1)
        .execute()
    )
    rows = existing.data or []
    if rows:
        return rows[0]
    inserted = (
        client.table("employee_daily_metrics")
        .insert(
            {
                "employee_id": employee_id,
                "work_date": day,
                "sd_cards_connected": 0,
                "footage_seconds_processed": 0,
            }
        )
        .execute()
    )
    rows = inserted.data or []
    if not rows:
        raise RuntimeError("Failed to create daily metrics row")
    return rows[0]


def _recompute_daily_metrics(employee_id: str, work_date: str) -> dict[str, Any]:
    """Roll up sessions + SD card events into employee_daily_metrics."""
    client = supabase_db.get_supabase()
    sessions = (
        client.table("employee_work_sessions")
        .select("start_time,end_time")
        .eq("employee_id", employee_id)
        .eq("work_date", work_date)
        .order("start_time")
        .execute()
    ).data or []
    events = (
        client.table("employee_sd_card_events")
        .select("footage_seconds")
        .eq("employee_id", employee_id)
        .eq("work_date", work_date)
        .execute()
    ).data or []

    start_time = None
    end_time = None
    for row in sessions:
        st = row.get("start_time")
        et = row.get("end_time")
        if st and (start_time is None or st < start_time):
            start_time = st
        if et and (end_time is None or et > end_time):
            end_time = et

    footage = sum(float(e.get("footage_seconds") or 0) for e in events)
    payload = {
        "start_time": start_time,
        "end_time": end_time,
        "sd_cards_connected": len(events),
        "footage_seconds_processed": footage,
        "updated_at": _iso(_now()),
    }
    existing = (
        client.table("employee_daily_metrics")
        .select("id")
        .eq("employee_id", employee_id)
        .eq("work_date", work_date)
        .limit(1)
        .execute()
    ).data or []
    if existing:
        result = (
            client.table("employee_daily_metrics")
            .update(payload)
            .eq("id", existing[0]["id"])
            .execute()
        )
    else:
        result = (
            client.table("employee_daily_metrics")
            .insert(
                {
                    "employee_id": employee_id,
                    "work_date": work_date,
                    **payload,
                }
            )
            .execute()
        )
    rows = result.data or []
    return rows[0] if rows else {"employee_id": employee_id, "work_date": work_date, **payload}


def start_work_session(employee_id: str) -> dict[str, Any]:
    """Open today's work session on login (idempotent if one is already open)."""
    day = supabase_db.today_date_str()
    client = supabase_db.get_supabase()
    ensure_daily_metrics(employee_id, day)

    open_sessions = (
        client.table("employee_work_sessions")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("work_date", day)
        .is_("end_time", "null")
        .order("start_time", desc=True)
        .limit(1)
        .execute()
    ).data or []
    if open_sessions:
        metrics = _recompute_daily_metrics(employee_id, day)
        return {"session": open_sessions[0], "metrics": metrics, "resumed": True}

    now = _now()
    inserted = (
        client.table("employee_work_sessions")
        .insert(
            {
                "employee_id": employee_id,
                "work_date": day,
                "start_time": _iso(now),
                "end_time": None,
            }
        )
        .execute()
    )
    rows = inserted.data or []
    if not rows:
        raise RuntimeError("Failed to start work session")
    metrics = _recompute_daily_metrics(employee_id, day)
    return {"session": rows[0], "metrics": metrics, "resumed": False}


def end_work_session(employee_id: str) -> dict[str, Any]:
    """Close any open work session for today on logout."""
    day = supabase_db.today_date_str()
    client = supabase_db.get_supabase()
    open_sessions = (
        client.table("employee_work_sessions")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("work_date", day)
        .is_("end_time", "null")
        .execute()
    ).data or []
    now = _iso(_now())
    closed = []
    for row in open_sessions:
        updated = (
            client.table("employee_work_sessions")
            .update({"end_time": now, "updated_at": now})
            .eq("id", row["id"])
            .execute()
        )
        closed.extend(updated.data or [])
    metrics = _recompute_daily_metrics(employee_id, day)
    return {"sessions": closed, "metrics": metrics}


def record_sd_card_connected(
    employee_id: str,
    *,
    card_id: str,
    card_path: str = "",
    camera_serial: str | None = None,
    footage_seconds: float = 0.0,
    video_count: int = 0,
) -> dict[str, Any]:
    """Record a unique SD card for this employee today (by C#### id)."""
    day = supabase_db.today_date_str()
    resolved_id = card_id_from_serial(camera_serial, card_id) or str(card_id or "").strip().upper()
    if not resolved_id:
        raise ValueError("card_id is required")

    client = supabase_db.get_supabase()
    ensure_daily_metrics(employee_id, day)
    existing = (
        client.table("employee_sd_card_events")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("work_date", day)
        .eq("card_id", resolved_id)
        .limit(1)
        .execute()
    ).data or []

    if existing:
        row = existing[0]
        updates: dict[str, Any] = {}
        if camera_serial and not row.get("camera_serial"):
            updates["camera_serial"] = camera_serial
        if card_path and not row.get("card_path"):
            updates["card_path"] = card_path
        if footage_seconds and float(row.get("footage_seconds") or 0) <= 0:
            updates["footage_seconds"] = float(footage_seconds)
        if video_count and int(row.get("video_count") or 0) <= 0:
            updates["video_count"] = int(video_count)
        if updates:
            updated = (
                client.table("employee_sd_card_events")
                .update(updates)
                .eq("id", row["id"])
                .execute()
            )
            row = (updated.data or [row])[0]
        metrics = _recompute_daily_metrics(employee_id, day)
        return {"event": row, "metrics": metrics, "already_exists": True}

    inserted = (
        client.table("employee_sd_card_events")
        .insert(
            {
                "employee_id": employee_id,
                "work_date": day,
                "card_id": resolved_id,
                "camera_serial": camera_serial,
                "card_path": card_path or "",
                "footage_seconds": float(footage_seconds or 0),
                "video_count": int(video_count or 0),
                "connected_at": _iso(_now()),
            }
        )
        .execute()
    )
    rows = inserted.data or []
    if not rows:
        raise RuntimeError("Failed to record SD card event")
    metrics = _recompute_daily_metrics(employee_id, day)
    return {"event": rows[0], "metrics": metrics, "already_exists": False}


def update_sd_card_footage(
    employee_id: str,
    card_id: str,
    *,
    footage_seconds: float,
    video_count: int | None = None,
    work_date: Optional[str] = None,
) -> dict[str, Any]:
    """Fill in probed duration for a card already connected today."""
    day = work_date or supabase_db.today_date_str()
    client = supabase_db.get_supabase()
    updates: dict[str, Any] = {"footage_seconds": float(footage_seconds or 0)}
    if video_count is not None:
        updates["video_count"] = int(video_count)
    result = (
        client.table("employee_sd_card_events")
        .update(updates)
        .eq("employee_id", employee_id)
        .eq("work_date", day)
        .eq("card_id", card_id)
        .execute()
    )
    rows = result.data or []
    metrics = _recompute_daily_metrics(employee_id, day)
    return {"event": rows[0] if rows else None, "metrics": metrics}


def get_today_metrics(employee_id: str) -> dict[str, Any]:
    day = supabase_db.today_date_str()
    metrics = ensure_daily_metrics(employee_id, day)
    # Always recompute so start/end/cards stay fresh.
    metrics = _recompute_daily_metrics(employee_id, day)
    client = supabase_db.get_supabase()
    cards = (
        client.table("employee_sd_card_events")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("work_date", day)
        .order("connected_at")
        .execute()
    ).data or []
    sessions = (
        client.table("employee_work_sessions")
        .select("*")
        .eq("employee_id", employee_id)
        .eq("work_date", day)
        .order("start_time")
        .execute()
    ).data or []
    footage_seconds = float(metrics.get("footage_seconds_processed") or 0)
    return {
        "work_date": day,
        "metrics": metrics,
        "cards": cards,
        "sessions": sessions,
        "footage_hours": round(footage_seconds / 3600.0, 3),
        "footage_label": _format_hours(footage_seconds),
    }


def _format_hours(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "0h 0m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"

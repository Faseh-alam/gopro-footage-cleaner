"""Supabase client and card / daily-summary persistence."""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_client = None
_env_loaded = False


def _load_env() -> None:
    global _env_loaded
    if _env_loaded:
        return
    load_dotenv(PROJECT_ROOT / ".env")
    _env_loaded = True


def supabase_configured() -> bool:
    _load_env()
    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip()
    return bool(url and key)


def get_supabase():
    """Return a cached Supabase client, or raise RuntimeError if not configured."""
    global _client
    _load_env()
    if _client is not None:
        return _client

    url = (os.environ.get("SUPABASE_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError(
            "Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY (or SUPABASE_SERVICE_ROLE_KEY) in .env"
        )

    from supabase import create_client

    _client = create_client(url, key)
    return _client


def today_date_str() -> str:
    return datetime.date.today().isoformat()


def _format_hours_mins(seconds: float) -> str:
    if not seconds or seconds < 0:
        return "0h 0m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m}m"


def find_card_today(card_name: str, work_date: Optional[str] = None) -> Optional[dict[str, Any]]:
    needle = card_name.strip().lower()
    if not needle:
        return None
    for card in list_cards_for_date(work_date):
        if str(card.get("card_name") or "").strip().lower() == needle:
            return card
    return None


def list_cards_for_date(work_date: Optional[str] = None) -> list[dict[str, Any]]:
    client = get_supabase()
    day = work_date or today_date_str()
    result = (
        client.table("cards")
        .select("*")
        .eq("work_date", day)
        .order("created_at")
        .execute()
    )
    return list(result.data or [])


def insert_card(row: dict[str, Any]) -> dict[str, Any]:
    client = get_supabase()
    result = client.table("cards").insert(row).execute()
    rows = result.data or []
    if not rows:
        raise RuntimeError("Failed to insert card into database")
    return rows[0]


def update_card(card_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    client = get_supabase()
    payload = {**updates, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    result = client.table("cards").update(payload).eq("id", card_id).execute()
    rows = result.data or []
    if not rows:
        raise RuntimeError("Failed to update card in database")
    return rows[0]


def ensure_daily_summary(work_date: Optional[str] = None) -> dict[str, Any]:
    """Ensure today's summary row exists.

    - No cards yet → empty zeros (create or reset stale counts).
    - Cards exist and summary missing → recompute from cards.
    - Cards exist and summary present → leave as-is (updates happen on register/finish).
    """
    day = work_date or today_date_str()
    client = get_supabase()
    cards = list_cards_for_date(day)
    existing = (
        client.table("daily_summaries")
        .select("*")
        .eq("work_date", day)
        .limit(1)
        .execute()
    )
    rows = existing.data or []

    if cards:
        if rows:
            return rows[0]
        return upsert_daily_summary(day)

    empty = {
        "work_date": day,
        "total_cards_received": 0,
        "total_used_space_before_gb": 0.0,
        "total_used_space_after_gb": 0.0,
        "total_storage_before_tb": 0.0,
        "total_storage_after_tb": 0.0,
        "total_original_duration_before": 0.0,
        "total_original_duration_after": 0.0,
        "total_hours_before": "0h 0m",
        "total_hours_after": "0h 0m",
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if rows:
        result = (
            client.table("daily_summaries")
            .update(empty)
            .eq("work_date", day)
            .execute()
        )
    else:
        result = client.table("daily_summaries").insert(empty).execute()
    out = result.data or []
    return out[0] if out else empty


def upsert_daily_summary(work_date: Optional[str] = None) -> dict[str, Any]:
    """Recompute today's summary from cards and upsert. Call on new card / finish."""
    day = work_date or today_date_str()
    cards = list_cards_for_date(day)

    space_before = 0.0
    space_after = 0.0
    duration_before = 0.0
    duration_after = 0.0

    for card in cards:
        space_before += float(card.get("used_space_before_labeling_gb") or 0)
        after = card.get("used_space_after_labeling_gb")
        if after is not None and after != "":
            space_after += float(after)
        duration_before += float(card.get("original_duration_before_labeling") or 0)
        after_dur = card.get("original_duration_after_labeling")
        if after_dur is not None and after_dur != "":
            duration_after += float(after_dur)

    summary = {
        "work_date": day,
        "total_cards_received": len(cards),
        "total_used_space_before_gb": round(space_before, 3),
        "total_used_space_after_gb": round(space_after, 3),
        "total_storage_before_tb": round(space_before / 1024.0, 4),
        "total_storage_after_tb": round(space_after / 1024.0, 4),
        "total_original_duration_before": round(duration_before, 3),
        "total_original_duration_after": round(duration_after, 3),
        "total_hours_before": _format_hours_mins(duration_before),
        "total_hours_after": _format_hours_mins(duration_after),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    client = get_supabase()
    existing = (
        client.table("daily_summaries")
        .select("id")
        .eq("work_date", day)
        .limit(1)
        .execute()
    )
    rows = existing.data or []
    if rows:
        result = (
            client.table("daily_summaries")
            .update(summary)
            .eq("work_date", day)
            .execute()
        )
    else:
        result = client.table("daily_summaries").insert(summary).execute()
    out = result.data or []
    return out[0] if out else summary


def test_connection() -> None:
    client = get_supabase()
    client.table("cards").select("id").limit(1).execute()

"""HTTP routes for card tracking and daily summaries (Supabase)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

from . import supabase_db
from .reporting import (
    card_stats,
    card_stats_light,
    _now_12h_time,
)
from .volumes import CARD_LABEL_RE, list_sd_cards

log = logging.getLogger(__name__)


def _card_public(row: dict[str, Any]) -> dict[str, Any]:
    """Shape DB row for the frontend (compatible with prior sheets responses)."""
    return {
        "id": row.get("id"),
        "cardName": row.get("card_name"),
        "card_name": row.get("card_name"),
        "cardPath": row.get("card_path"),
        "card_path": row.get("card_path"),
        "date": row.get("work_date"),
        "work_date": row.get("work_date"),
        "insert_time": row.get("insert_time") or "",
        "finish_time": row.get("finish_time") or "",
        "status": row.get("status") or "",
        "total_mp4_videos": row.get("total_mp4_videos"),
        "original_duration": row.get("original_duration"),
        "final_duration": row.get("final_duration"),
        "duration_difference": row.get("duration_difference"),
        "card_capacity": row.get("card_capacity"),
        "used_space": row.get("used_space"),
        "used_space_before_labeling_gb": row.get("used_space_before_labeling_gb"),
        "used_space_after_labeling_gb": row.get("used_space_after_labeling_gb"),
        "original_duration_before_labeling": row.get("original_duration_before_labeling"),
        "original_duration_after_labeling": row.get("original_duration_after_labeling"),
    }


def _fill_insert_durations(card_id: str, card_path: str, work_date: str) -> None:
    """Probe video durations after a fast insert; then refresh today's summary."""
    try:
        stats = card_stats(card_path, probe_durations=True)
        supabase_db.update_card(
            card_id,
            {
                "original_duration": stats["original_duration"],
                "original_duration_before_labeling": stats["original_duration"],
                "total_mp4_videos": stats["total_mp4_videos"],
                "used_space": stats["used_space_before_labeling_gb"],
                "used_space_before_labeling_gb": stats["used_space_before_labeling_gb"],
                "card_capacity": stats["card_capacity"],
            },
        )
        supabase_db.upsert_daily_summary(work_date)
    except Exception:
        log.exception("Failed to fill durations for card %s", card_id)


def create_cards_blueprint() -> Blueprint:
    bp = Blueprint("cards", __name__, url_prefix="/api/cards")

    @bp.route("/status", methods=["GET"])
    def status():
        configured = supabase_db.supabase_configured()
        return jsonify({
            "configured": configured,
            "ok": configured,
        })

    @bp.route("/test", methods=["GET"])
    def test_connection():
        if not supabase_db.supabase_configured():
            return jsonify({
                "ok": False,
                "error": "Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY in .env",
            }), 400
        try:
            supabase_db.test_connection()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @bp.route("/today", methods=["GET"])
    def get_today():
        if not supabase_db.supabase_configured():
            return jsonify({"error": "Supabase is not configured"}), 503
        try:
            day = supabase_db.today_date_str()
            # Always ensure a daily summary row exists for today (even with 0 cards).
            summary = supabase_db.ensure_daily_summary(day)
            cards = [_card_public(c) for c in supabase_db.list_cards_for_date(day)]
            return jsonify({
                "date": day,
                "cards": cards,
                "summary": summary,
                "currentProcess": {
                    "sheetName": day,
                    "cards": cards,
                },
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @bp.route("/register", methods=["POST"])
    def add_card():
        """First encounter today: insert quickly; skip if card already saved for today."""
        if not supabase_db.supabase_configured():
            return jsonify({"error": "Supabase is not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON payload required"}), 400
        card_path = str(data.get("cardPath", "")).strip()
        card_name = str(data.get("cardName", "")).strip()
        if not card_path or not card_name:
            return jsonify({"error": "cardPath and cardName are required"}), 400

        # Only real detected GoPro SD cards (C####) may be registered — never
        # arbitrary folders / debug paths.
        if not CARD_LABEL_RE.match(card_name):
            return jsonify({
                "error": f"Invalid card name '{card_name}'. Expected a detected SD card like C1234.",
            }), 400
        try:
            detected = list_sd_cards()
        except Exception as e:
            return jsonify({"error": f"Failed to detect SD cards: {e}"}), 500
        connected = next(
            (
                c for c in detected
                if str(c.get("id") or "").upper() == card_name.upper()
            ),
            None,
        )
        if not connected:
            return jsonify({
                "error": f"SD card '{card_name}' is not currently connected.",
            }), 400
        # Prefer the detector's paths so clients can't register arbitrary folders.
        card_path = str(connected.get("scan_path") or connected.get("path") or card_path).strip()

        try:
            existing = supabase_db.find_card_today(card_name)
            if existing:
                return jsonify({
                    "ok": True,
                    "already_exists": True,
                    "card": _card_public(existing),
                    "message": f"Card '{card_name}' already exists for today",
                }), 200

            # Fast path — used space / counts only. Duration probing is slow (ffprobe
            # per video) and previously blocked the request long enough that nothing
            # appeared to save.
            stats = card_stats_light(card_path)
            day = supabase_db.today_date_str()
            resolved_path = str(Path(card_path).expanduser().resolve())
            row = {
                "work_date": day,
                "card_name": card_name,
                "card_path": resolved_path,
                "insert_time": _now_12h_time(),
                "finish_time": "",
                "total_mp4_videos": stats["total_mp4_videos"],
                "original_duration": 0.0,
                "final_duration": None,
                "duration_difference": None,
                "card_capacity": stats["card_capacity"],
                "used_space": stats["used_space_before_labeling_gb"],
                "status": "Pending",
                "used_space_before_labeling_gb": stats["used_space_before_labeling_gb"],
                "used_space_after_labeling_gb": None,
                "original_duration_before_labeling": 0.0,
                "original_duration_after_labeling": None,
            }
            inserted = supabase_db.insert_card(row)
            summary = supabase_db.upsert_daily_summary(day)

            threading.Thread(
                target=_fill_insert_durations,
                args=(str(inserted["id"]), resolved_path, day),
                daemon=True,
                name=f"card-duration-{card_name}",
            ).start()

            return jsonify({
                "ok": True,
                "already_exists": False,
                "card": _card_public(inserted),
                "summary": summary,
            })
        except Exception as e:
            log.exception("add_card failed")
            return jsonify({"error": str(e)}), 500

    @bp.route("/finish", methods=["POST"])
    def finish_card():
        """Update remaining fields when the user finishes the card; refresh summary."""
        if not supabase_db.supabase_configured():
            return jsonify({"error": "Supabase is not configured"}), 503

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "JSON payload required"}), 400
        card_name = str(data.get("cardName", "")).strip()
        if not card_name:
            return jsonify({"error": "cardName is required"}), 400

        try:
            found = supabase_db.find_card_today(card_name)
            if not found:
                return jsonify({"error": f"Card '{card_name}' not found for today"}), 404

            card_path_str = str(found.get("card_path") or "").strip()
            if not card_path_str:
                return jsonify({"error": "Card path is empty in database"}), 400

            # After-labeling snapshot (may be slow due to ffprobe — only runs on finish).
            stats = card_stats(card_path_str, probe_durations=True)
            finish_time = _now_12h_time()
            final_duration = float(stats["final_duration"] or 0)
            original_before = float(
                found.get("original_duration_before_labeling")
                or found.get("original_duration")
                or 0
            )
            # If insert-time duration probe had not finished yet, capture current as before.
            if original_before <= 0:
                original_before = float(stats["original_duration"] or 0)

            duration_diff = round(original_before - final_duration, 3)

            updates = {
                "finish_time": finish_time,
                "final_duration": final_duration,
                "duration_difference": duration_diff,
                "status": "Completed",
                "card_capacity": stats["card_capacity"],
                "used_space_after_labeling_gb": stats["used_space_after_labeling_gb"],
                "original_duration_after_labeling": final_duration,
                "original_duration": original_before,
                "original_duration_before_labeling": original_before,
            }
            updated = supabase_db.update_card(str(found["id"]), updates)
            summary = supabase_db.upsert_daily_summary(
                str(found.get("work_date") or supabase_db.today_date_str())
            )
            return jsonify({
                "ok": True,
                "card": _card_public(updated),
                "summary": summary,
            })
        except Exception as e:
            log.exception("finish_card failed")
            return jsonify({"error": str(e)}), 500

    return bp

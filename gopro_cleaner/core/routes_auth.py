"""HTTP routes for employee login / signup and daily metrics."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import auth_service, employee_metrics, supabase_db


def create_auth_blueprint() -> Blueprint:
    bp = Blueprint("auth", __name__, url_prefix="/api/auth")

    # Local / ScaleAI stations: skip Supabase entirely so missing packages
    # never break the review UI. Flip to False when cloud login is needed again.
    AUTH_DISABLED = True

    def _auth_error(exc: Exception):
        if isinstance(exc, PermissionError):
            return jsonify({"error": str(exc)}), 401
        if isinstance(exc, ValueError):
            return jsonify({"error": str(exc)}), 400
        if isinstance(exc, RuntimeError) and "not configured" in str(exc).lower():
            return jsonify({"error": str(exc)}), 503
        return jsonify({"error": str(exc)}), 500

    @bp.get("/status")
    def auth_status():
        if AUTH_DISABLED:
            return jsonify({"configured": False, "ok": False, "disabled": True})
        return jsonify(
            {
                "configured": supabase_db.supabase_configured(),
                "ok": supabase_db.supabase_configured(),
                "disabled": False,
            }
        )

    @bp.post("/signup")
    def signup():
        if AUTH_DISABLED:
            return jsonify({"error": "Login is disabled on this station", "disabled": True}), 503
        if not supabase_db.supabase_configured():
            return jsonify({"error": "Supabase is not configured"}), 503
        data = request.get_json(silent=True) or {}
        try:
            result = auth_service.signup(
                str(data.get("email") or ""),
                str(data.get("password") or ""),
                str(data.get("full_name") or data.get("name") or ""),
            )
            employee_id = result["user"]["id"]
            session_info = employee_metrics.start_work_session(employee_id)
            return jsonify(
                {
                    "ok": True,
                    **result,
                    "work": session_info,
                    "today": employee_metrics.get_today_metrics(employee_id),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _auth_error(exc)

    @bp.post("/login")
    def login():
        if AUTH_DISABLED:
            return jsonify({"error": "Login is disabled on this station", "disabled": True}), 503
        if not supabase_db.supabase_configured():
            return jsonify({"error": "Supabase is not configured"}), 503
        data = request.get_json(silent=True) or {}
        try:
            result = auth_service.login(
                str(data.get("email") or ""),
                str(data.get("password") or ""),
            )
            employee_id = result["user"]["id"]
            session_info = employee_metrics.start_work_session(employee_id)
            return jsonify(
                {
                    "ok": True,
                    **result,
                    "work": session_info,
                    "today": employee_metrics.get_today_metrics(employee_id),
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _auth_error(exc)

    @bp.post("/refresh")
    def refresh():
        if not supabase_db.supabase_configured():
            return jsonify({"error": "Supabase is not configured"}), 503
        data = request.get_json(silent=True) or {}
        try:
            result = auth_service.refresh_session(str(data.get("refresh_token") or ""))
            return jsonify({"ok": True, **result})
        except Exception as exc:  # noqa: BLE001
            return _auth_error(exc)

    @bp.get("/me")
    def me():
        try:
            data = auth_service.require_employee()
            today = employee_metrics.get_today_metrics(data["user"]["id"])
            return jsonify({"ok": True, **data, "today": today})
        except Exception as exc:  # noqa: BLE001
            return _auth_error(exc)

    @bp.post("/logout")
    def logout():
        try:
            data = auth_service.require_employee()
            closed = employee_metrics.end_work_session(data["user"]["id"])
            return jsonify({"ok": True, "work": closed, "today": closed.get("metrics")})
        except Exception as exc:  # noqa: BLE001
            return _auth_error(exc)

    @bp.get("/metrics/today")
    def metrics_today():
        try:
            data = auth_service.require_employee()
            return jsonify({"ok": True, **employee_metrics.get_today_metrics(data["user"]["id"])})
        except Exception as exc:  # noqa: BLE001
            return _auth_error(exc)

    return bp

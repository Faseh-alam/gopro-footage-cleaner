"""Employee authentication via Supabase Auth (Flask-side)."""

from __future__ import annotations

import os
import re
from typing import Any, Optional

from flask import g, request

from . import supabase_db

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _client():
    return supabase_db.get_supabase()


def _user_to_dict(user: Any) -> dict[str, Any]:
    meta = getattr(user, "user_metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "id": str(getattr(user, "id", "") or ""),
        "email": str(getattr(user, "email", "") or ""),
        "full_name": str(meta.get("full_name") or meta.get("name") or "").strip(),
    }


def _session_to_dict(session: Any) -> dict[str, Any]:
    return {
        "access_token": str(getattr(session, "access_token", "") or ""),
        "refresh_token": str(getattr(session, "refresh_token", "") or ""),
        "expires_in": getattr(session, "expires_in", None),
        "token_type": str(getattr(session, "token_type", "") or "bearer"),
    }


def ensure_employee_row(user_id: str, email: str, full_name: str = "") -> dict[str, Any]:
    """Ensure the public.employees profile exists for an auth user."""
    client = _client()
    found = (
        client.table("employees")
        .select("*")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = found.data or []
    if rows:
        row = rows[0]
        updates: dict[str, Any] = {}
        if email and row.get("email") != email:
            updates["email"] = email
        if full_name and not (row.get("full_name") or "").strip():
            updates["full_name"] = full_name
        if not updates:
            return row
        updated = (
            client.table("employees")
            .update(updates)
            .eq("id", user_id)
            .execute()
        )
        return (updated.data or [row])[0]

    inserted = (
        client.table("employees")
        .insert(
            {
                "id": user_id,
                "email": email,
                "full_name": full_name or "",
            }
        )
        .execute()
    )
    rows = inserted.data or []
    if not rows:
        raise RuntimeError("Could not create employee profile")
    return rows[0]


def get_employee(user_id: str) -> Optional[dict[str, Any]]:
    client = _client()
    result = client.table("employees").select("*").eq("id", user_id).limit(1).execute()
    rows = result.data or []
    return rows[0] if rows else None


def signup(email: str, password: str, full_name: str = "") -> dict[str, Any]:
    email = email.strip().lower()
    full_name = full_name.strip()
    if not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address")
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters")
    if not full_name:
        raise ValueError("Full name is required")

    client = _client()
    user = None
    session = None

    # Prefer admin create so employees can sign in immediately (no email confirm).
    supabase_db._load_env()
    if (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip():
        try:
            created = client.auth.admin.create_user(
                {
                    "email": email,
                    "password": password,
                    "email_confirm": True,
                    "user_metadata": {"full_name": full_name},
                }
            )
            user = getattr(created, "user", None) or created
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "already" in msg or "registered" in msg or "exists" in msg:
                raise ValueError("An account with this email already exists") from exc
            # Fall through to public sign_up if admin API unavailable.
            user = None

    if user is None:
        try:
            result = client.auth.sign_up(
                {
                    "email": email,
                    "password": password,
                    "options": {"data": {"full_name": full_name}},
                }
            )
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "already" in msg or "registered" in msg:
                raise ValueError("An account with this email already exists") from exc
            if "rate limit" in msg:
                raise ValueError(
                    "Supabase email signup is rate-limited (usually after a few tries per hour). "
                    "Wait ~30–60 minutes, or add SUPABASE_SERVICE_ROLE_KEY to .env and restart — "
                    "that creates accounts without sending confirmation emails."
                ) from exc
            raise RuntimeError(str(exc)) from exc
        user = getattr(result, "user", None)
        session = getattr(result, "session", None)

    if user is None:
        raise RuntimeError("Signup failed — check Supabase Auth settings")

    user_dict = _user_to_dict(user)
    if not user_dict.get("full_name"):
        user_dict["full_name"] = full_name
    employee = ensure_employee_row(user_dict["id"], user_dict["email"], user_dict["full_name"])

    if session is None:
        # Admin create / confirm-required signups don't return a session.
        try:
            return login(email, password)
        except ValueError as exc:
            raise RuntimeError(
                "Account created but email confirmation is required. "
                "In Supabase → Authentication → Providers → Email, disable "
                "\"Confirm email\", or set SUPABASE_SERVICE_ROLE_KEY in .env "
                "so signup can confirm users automatically."
            ) from exc

    return {
        "user": {**user_dict, "full_name": employee.get("full_name") or full_name},
        "employee": employee,
        "session": _session_to_dict(session),
    }


def login(email: str, password: str) -> dict[str, Any]:
    email = email.strip().lower()
    if not email or not password:
        raise ValueError("Email and password are required")
    client = _client()
    try:
        result = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid email or password") from exc
    user = getattr(result, "user", None)
    session = getattr(result, "session", None)
    if user is None or session is None:
        raise ValueError("Invalid email or password")
    user_dict = _user_to_dict(user)
    employee = ensure_employee_row(
        user_dict["id"], user_dict["email"], user_dict.get("full_name") or ""
    )
    return {
        "user": {
            **user_dict,
            "full_name": employee.get("full_name") or user_dict.get("full_name") or "",
        },
        "employee": employee,
        "session": _session_to_dict(session),
    }


def refresh_session(refresh_token: str) -> dict[str, Any]:
    if not refresh_token:
        raise ValueError("refresh_token is required")
    client = _client()
    try:
        result = client.auth.refresh_session(refresh_token)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Session expired — please log in again") from exc
    user = getattr(result, "user", None)
    session = getattr(result, "session", None)
    if user is None or session is None:
        raise ValueError("Session expired — please log in again")
    user_dict = _user_to_dict(user)
    employee = ensure_employee_row(
        user_dict["id"], user_dict["email"], user_dict.get("full_name") or ""
    )
    return {
        "user": {
            **user_dict,
            "full_name": employee.get("full_name") or user_dict.get("full_name") or "",
        },
        "employee": employee,
        "session": _session_to_dict(session),
    }


def user_from_access_token(access_token: str) -> dict[str, Any]:
    if not access_token:
        raise ValueError("Missing access token")
    client = _client()
    try:
        result = client.auth.get_user(access_token)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Invalid or expired session") from exc
    user = getattr(result, "user", None) or result
    if user is None or not getattr(user, "id", None):
        raise ValueError("Invalid or expired session")
    user_dict = _user_to_dict(user)
    employee = ensure_employee_row(
        user_dict["id"], user_dict["email"], user_dict.get("full_name") or ""
    )
    return {
        "user": {
            **user_dict,
            "full_name": employee.get("full_name") or user_dict.get("full_name") or "",
        },
        "employee": employee,
    }


def bearer_token_from_request() -> str:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # Also allow JSON body / query for logout convenience.
    payload = request.get_json(silent=True) or {}
    return str(payload.get("access_token") or request.args.get("access_token") or "").strip()


def require_employee() -> dict[str, Any]:
    """Resolve the logged-in employee from Authorization: Bearer <jwt>."""
    if not supabase_db.supabase_configured():
        raise RuntimeError("Supabase is not configured")
    token = bearer_token_from_request()
    if not token:
        raise PermissionError("Login required")
    data = user_from_access_token(token)
    g.employee = data["employee"]
    g.auth_user = data["user"]
    g.access_token = token
    return data

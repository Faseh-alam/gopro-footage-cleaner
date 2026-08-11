"""Smoke-test auth helpers + card id derivation (no live signup required)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from gopro_cleaner.core.card_identity import card_id_from_serial  # noqa: E402
from gopro_cleaner.app import create_app  # noqa: E402

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


check("serial → C####", card_id_from_serial("C350132450712") == "C0712")
check("short serial ignored", card_id_from_serial("AB12", "C9999") == "C9999")
check("fallback badge", card_id_from_serial("", "c1234") == "C1234")
check("empty → empty", card_id_from_serial("") == "")

app = create_app()
client = app.test_client()

r = client.get("/api/auth/status")
body = r.get_json() or {}
check("GET /api/auth/status", r.status_code == 200 and "configured" in body, str(body))

r = client.get("/api/auth/me")
check("GET /api/auth/me without token → 401", r.status_code == 401, str(r.status_code))

r = client.post("/api/auth/login", json={"email": "x", "password": "y"})
# 400 validation or 503 if supabase missing — never 500 from missing route
check(
    "POST /api/auth/login routed",
    r.status_code in {400, 401, 503},
    f"{r.status_code} {r.get_json()}",
)

r = client.post(
    "/api/auth/signup",
    json={"email": "bad", "password": "123", "full_name": ""},
)
check(
    "POST /api/auth/signup validation",
    r.status_code in {400, 503},
    f"{r.status_code} {r.get_json()}",
)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("Auth / metrics smoke checks passed.")

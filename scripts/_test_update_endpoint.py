"""Sanity checks for POST /api/update (no real pull/restart is triggered).

With a dirty working tree the endpoint must refuse with 400 and leave
everything untouched — which doubles as a safe route-level test.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from gopro_cleaner.core import self_update  # noqa: E402

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


dirty = self_update._dirty_tracked_files()
check("dirty tracked files detected (dev tree)", bool(dirty), ", ".join(dirty[:4]))
check(
    "preserved config not counted as dirty",
    "sd_offloader/config.json" not in dirty,
)

branch = self_update.current_branch()
check("current branch resolves", branch == "redesign", branch)

from gopro_cleaner.app import create_app  # noqa: E402

client = create_app().test_client()
resp = client.post("/api/update")
body = resp.get_json() or {}
check("dirty tree → 400", resp.status_code == 400, str(resp.status_code))
check(
    "refusal message explains why",
    "Local code changes detected" in str(body.get("error") or ""),
    str(body.get("error") or "")[:90],
)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("Update endpoint sanity checks passed.")

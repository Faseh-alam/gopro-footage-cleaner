"""Sanity checks for the update helpers (does not pull or restart)."""

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
check(
    "preserved config not counted as dirty",
    "sd_offloader/config.json" not in dirty,
    ", ".join(dirty[:4]) if dirty else "clean tree",
)

branch = self_update.current_branch()
check("current branch resolves", bool(branch), branch)

status = self_update.check_for_updates()
check("update check runs", bool(status.get("ok") or status.get("error")), str(status)[:120])

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    raise SystemExit(1)
print("Update helper sanity checks passed.")

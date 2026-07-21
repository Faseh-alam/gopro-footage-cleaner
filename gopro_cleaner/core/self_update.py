"""One-click updater: pull latest main from GitHub, then relaunch the app.

Made for operators who don't use git — the Update button in the review UI
calls this. Machine-local settings (offloader config) are preserved across
the hard reset to origin/main.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Tracked files that machines customize locally — kept across updates.
PRESERVE_FILES = ("sd_offloader/config.json",)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git failed").strip()
        raise RuntimeError(detail[-500:])
    return (result.stdout or "").strip()


def _dirty_tracked_files() -> list[str]:
    """Tracked files with local modifications, ignoring the preserved ones."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "git status failed").strip()[-500:])
    dirty = []
    for line in (result.stdout or "").splitlines():
        if len(line) < 4 or line.startswith("??"):
            continue
        rel = line[3:].strip().strip('"')
        if rel in PRESERVE_FILES:
            continue
        dirty.append(rel)
    return dirty


def pull_latest_main() -> dict:
    """Fetch origin/main and hard-reset to it. Returns before/after commits."""
    if not shutil.which("git"):
        raise RuntimeError("git is not installed on this computer — install Git for Windows first")
    if not (PROJECT_ROOT / ".git").exists():
        raise RuntimeError("This folder is not a git checkout — reinstall from GitHub")

    dirty = _dirty_tracked_files()
    if dirty:
        preview = ", ".join(dirty[:5]) + ("…" if len(dirty) > 5 else "")
        raise RuntimeError(
            f"Local code changes detected ({preview}) — update refused so nothing is lost. "
            "Ask the developer to update this machine."
        )

    preserved: dict[str, bytes] = {}
    for rel in PRESERVE_FILES:
        path = PROJECT_ROOT / rel
        if path.is_file():
            preserved[rel] = path.read_bytes()

    before = _git("rev-parse", "HEAD")
    _git("fetch", "origin", "main")
    _git("checkout", "-f", "main")
    _git("reset", "--hard", "origin/main")
    after = _git("rev-parse", "HEAD")

    for rel, data in preserved.items():
        try:
            (PROJECT_ROOT / rel).write_bytes(data)
        except OSError:
            pass

    return {
        "before": before[:7],
        "after": after[:7],
        "changed": before != after,
    }


def relaunch_and_exit(delay_seconds: float = 1.5) -> None:
    """Spawn a detached relauncher (run.bat / run.sh), then exit this process.

    The run scripts already free the port and reinstall dependencies, so the
    fresh process comes up on the new code. The response for the current HTTP
    request is flushed during the delay before os._exit.
    """

    def _go() -> None:
        time.sleep(0.3)
        try:
            if platform.system() == "Windows":
                script = PROJECT_ROOT / "run.bat"
                subprocess.Popen(
                    ["cmd", "/c", f'timeout /t 2 /nobreak >nul & "{script}"'],
                    cwd=str(PROJECT_ROOT),
                    creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
                    close_fds=True,
                )
            else:
                script = PROJECT_ROOT / "run.sh"
                subprocess.Popen(
                    ["bash", "-c", f'sleep 2; exec "{script}"'],
                    cwd=str(PROJECT_ROOT),
                    start_new_session=True,
                    close_fds=True,
                )
        finally:
            time.sleep(delay_seconds)
            os._exit(0)

    threading.Thread(target=_go, daemon=True, name="self-update-relaunch").start()

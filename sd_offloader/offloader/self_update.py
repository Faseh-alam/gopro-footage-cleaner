"""One-click updater for the SD Card Offloader.

Pulls the currently checked-out git branch, then relaunches this app
(run.bat / run.sh). Machine-local settings (config.json) are preserved.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

OFFLOADER_ROOT = Path(__file__).resolve().parents[1]


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    return here.parents[2]


PROJECT_ROOT = _repo_root()

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


def current_branch() -> str:
    """Return the checked-out local branch; detached HEAD is unsafe to update."""
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD").strip()
    if not branch:
        raise RuntimeError(
            "This checkout is not on a branch — ask the developer to select the right branch"
        )
    return branch


def pull_latest_current_branch() -> dict:
    """Fetch origin/<current-branch> and reset to it only when GitHub is ahead.

    Local uncommitted files are never a reason to refuse. They are also not
    inspected: if origin already matches HEAD, the working tree is left as-is.
    If origin has new commits, this hard-resets to that (config.json kept).
    """
    if not shutil.which("git"):
        raise RuntimeError("git is not installed on this computer — install Git for Windows first")
    if not (PROJECT_ROOT / ".git").exists():
        raise RuntimeError("This folder is not a git checkout — reinstall from GitHub")

    branch = current_branch()
    before = _git("rev-parse", "HEAD")
    _git("fetch", "origin", branch)
    remote = _git("rev-parse", f"origin/{branch}")
    if before == remote:
        return {
            "branch": branch,
            "before": before[:7],
            "after": remote[:7],
            "changed": False,
        }

    preserved: dict[str, bytes] = {}
    for rel in PRESERVE_FILES:
        path = PROJECT_ROOT / rel
        if path.is_file():
            preserved[rel] = path.read_bytes()

    _git("reset", "--hard", f"origin/{branch}")
    after = _git("rev-parse", "HEAD")

    for rel, data in preserved.items():
        try:
            (PROJECT_ROOT / rel).write_bytes(data)
        except OSError:
            pass

    return {
        "branch": branch,
        "before": before[:7],
        "after": after[:7],
        "changed": True,
    }


def check_for_updates() -> dict:
    """Compare local HEAD to origin/<branch> after a quiet fetch."""
    if not shutil.which("git") or not (PROJECT_ROOT / ".git").exists():
        return {"ok": False, "behind": False, "error": "git unavailable"}
    try:
        branch = current_branch()
        local = _git("rev-parse", "HEAD")
        try:
            _git("fetch", "origin", branch)
        except RuntimeError:
            return {
                "ok": True,
                "behind": False,
                "branch": branch,
                "local": local[:7],
                "remote": local[:7],
                "offline": True,
            }
        remote = _git("rev-parse", f"origin/{branch}")
        return {
            "ok": True,
            "behind": local != remote,
            "branch": branch,
            "local": local[:7],
            "remote": remote[:7],
            "offline": False,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "behind": False, "error": str(exc)}


def relaunch_and_exit(delay_seconds: float = 1.5) -> None:
    """Spawn a detached relauncher (run.bat / run.sh), then exit this process.

    The run scripts already free the port and reinstall dependencies, so the
    fresh process comes up on the new code. The response for the current HTTP
    request is flushed during the delay before os._exit.
    """

    def _go() -> None:
        time.sleep(0.3)
        # The already-open tab reloads itself once the server is back — the
        # relaunched run script must not open a second browser tab.
        env = {**os.environ, "SD_OFFLOADER_OPEN_BROWSER": "0"}
        try:
            if platform.system() == "Windows":
                script = OFFLOADER_ROOT / "run.bat"
                subprocess.Popen(
                    ["cmd", "/c", f'timeout /t 2 /nobreak >nul & "{script}"'],
                    cwd=str(OFFLOADER_ROOT),
                    creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
                    close_fds=True,
                    env=env,
                )
            else:
                script = OFFLOADER_ROOT / "run.sh"
                subprocess.Popen(
                    ["bash", "-c", f'sleep 2; exec "{script}"'],
                    cwd=str(OFFLOADER_ROOT),
                    start_new_session=True,
                    close_fds=True,
                    env=env,
                )
        finally:
            time.sleep(delay_seconds)
            os._exit(0)

    threading.Thread(target=_go, daemon=True, name="self-update-relaunch").start()

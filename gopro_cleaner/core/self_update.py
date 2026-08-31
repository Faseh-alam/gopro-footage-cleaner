"""One-click updater: pull the currently checked-out branch, then relaunch.

Made for operators who don't use git — the Update button in the review UI
calls this. Machine-local settings (offloader config) are preserved across
the hard reset to the matching branch on ``origin``.
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


def _git_env() -> dict[str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return env


def _friendly_git_error(detail: str) -> str:
    text = (detail or "git failed").strip()
    lowered = text.lower()
    if (
        "could not read username" in lowered
        or "authentication failed" in lowered
        or "permission denied (publickey)" in lowered
        or "could not read password" in lowered
    ):
        return (
            "GitHub login is missing on this computer. Open Terminal in the project folder, "
            "run: git fetch origin — sign in if asked — then click Update again."
        )
    if "could not resolve host" in lowered or "failed to connect" in lowered:
        return "Could not reach GitHub. Check the internet connection, then click Update again."
    if "couldn't find remote ref" in lowered or "couldn't find remote" in lowered:
        return "This branch is not on GitHub yet. Push it from the developer PC, then click Update."
    return text[-500:]


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=_git_env(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git failed").strip()
        raise RuntimeError(_friendly_git_error(detail))
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


def current_branch() -> str:
    """Return the checked-out local branch; detached HEAD is unsafe to update."""
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD").strip()
    if not branch:
        raise RuntimeError(
            "This checkout is not on a branch — ask the developer to select main or testing"
        )
    return branch


def pull_latest_current_branch() -> dict:
    """Fetch and hard-reset the current branch to match GitHub.

    Local code edits in this project folder are overwritten on purpose — that is
    what the Update button is for. Footage, labels, and clips live outside git
    and are not touched. Machine-local ``sd_offloader/config.json`` is restored.
    """
    if not shutil.which("git"):
        raise RuntimeError("git is not installed on this computer — install Git, then click Update again")
    if not (PROJECT_ROOT / ".git").exists():
        raise RuntimeError("This folder is not a git checkout — reinstall from GitHub")

    preserved: dict[str, bytes] = {}
    for rel in PRESERVE_FILES:
        path = PROJECT_ROOT / rel
        if path.is_file():
            preserved[rel] = path.read_bytes()

    branch = current_branch()
    before = _git("rev-parse", "HEAD")
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    try:
        _git("fetch", "origin", refspec)
    except RuntimeError:
        _git("fetch", "origin", branch)
    try:
        _git("reset", "--hard", f"origin/{branch}")
    except RuntimeError:
        _git("reset", "--hard", "FETCH_HEAD")
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
        "changed": before != after,
    }


def pull_latest_main() -> dict:
    """Backward-compatible route helper; now updates the current branch."""
    return pull_latest_current_branch()


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
            # Offline / no remote — still report local sha.
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


def current_git_sha() -> str:
    try:
        return _git("rev-parse", "--short", "HEAD")
    except Exception:
        return ""


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
        env = {**os.environ, "GOPRO_NO_BROWSER": "1"}
        try:
            if platform.system() == "Windows":
                script = PROJECT_ROOT / "run.bat"
                subprocess.Popen(
                    ["cmd", "/c", f'timeout /t 2 /nobreak >nul & "{script}"'],
                    cwd=str(PROJECT_ROOT),
                    creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
                    close_fds=True,
                    env=env,
                )
            else:
                script = PROJECT_ROOT / "run.sh"
                subprocess.Popen(
                    ["bash", "-c", f'sleep 2; exec "{script}"'],
                    cwd=str(PROJECT_ROOT),
                    start_new_session=True,
                    close_fds=True,
                    env=env,
                )
        finally:
            time.sleep(delay_seconds)
            os._exit(0)

    threading.Thread(target=_go, daemon=True, name="self-update-relaunch").start()

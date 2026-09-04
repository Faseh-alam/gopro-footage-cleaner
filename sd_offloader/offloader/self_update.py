"""One-click updater for the SD Card Offloader.

Pulls the currently checked-out git branch, then relaunches this app
(run.bat / run.sh). Machine-local settings (config.json) are preserved.

A GitHub copy that does not even parse (IndentationError in engine.py) is
refused so Update cannot replace a working tree with a crash.
"""

from __future__ import annotations

import ast
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
PRESERVE_FILES = ("sd_offloader/config.json", "config.json")

# Must parse before a hard reset, or run.bat dies on IndentationError.
_PARSE_TARGETS = (
    "sd_offloader/offloader/engine.py",
    "sd_offloader/offloader/app.py",
    "sd_offloader/offloader/aws_upload.py",
    "offloader/engine.py",
    "offloader/app.py",
)


def _git(*args: str, timeout: int = 20) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(args[:3])} timed out after {timeout}s") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git failed").strip()
        raise RuntimeError(detail[-500:])
    return (result.stdout or "").strip()


def _fetch_branch(branch: str) -> None:
    """No-tags fetch — a full fetch was hanging the Update button for minutes."""
    _git("fetch", "--no-tags", "--prune", "origin", branch, timeout=45)


def _repo_rel_candidates() -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for rel in _PARSE_TARGETS:
        key = Path(rel).name
        if key in seen:
            continue
        path = PROJECT_ROOT / rel
        if path.is_file():
            found.append(rel.replace("\\", "/"))
            seen.add(key)
    if not found:
        found = ["sd_offloader/offloader/engine.py"]
    return found


def _assert_python_parses(source: str, label: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        raise RuntimeError(
            f"Update blocked — {label} is not valid Python "
            f"(line {exc.lineno}: {exc.msg}). "
            "GitHub was not applied. Copy engine.py from the working machine, "
            "or ask the developer to push a compiling commit."
        ) from exc


def _assert_remote_parses(branch: str) -> None:
    for rel in _repo_rel_candidates():
        try:
            source = _git("show", f"origin/{branch}:{rel}")
        except RuntimeError:
            continue
        _assert_python_parses(source, f"origin/{branch}:{rel}")


def _assert_workdir_parses() -> None:
    for rel in _repo_rel_candidates():
        path = PROJECT_ROOT / rel
        if path.is_file():
            _assert_python_parses(path.read_text(encoding="utf-8"), rel)


def current_branch() -> str:
    """Return the checked-out local branch; detached HEAD is unsafe to update."""
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD").strip()
    if not branch:
        raise RuntimeError(
            "This checkout is not on a branch — ask the developer to select the right branch"
        )
    return branch


def pull_latest_current_branch() -> dict:
    """Fetch origin/<current-branch> and reset to it only when GitHub is ahead
    and the remote Python files still parse.

    Local config.json is preserved. A broken GitHub engine.py is refused so
    run.bat does not start with IndentationError.
    """
    if not shutil.which("git"):
        raise RuntimeError("git is not installed on this computer — install Git for Windows first")
    if not (PROJECT_ROOT / ".git").exists():
        raise RuntimeError("This folder is not a git checkout — reinstall from GitHub")

    branch = current_branch()
    before = _git("rev-parse", "HEAD")
    _fetch_branch(branch)
    remote = _git("rev-parse", f"origin/{branch}")
    if before == remote:
        return {
            "branch": branch,
            "before": before[:7],
            "after": remote[:7],
            "changed": False,
        }

    _assert_remote_parses(branch)

    preserved: dict[str, bytes] = {}
    for rel in PRESERVE_FILES:
        path = PROJECT_ROOT / rel
        if path.is_file():
            preserved[rel] = path.read_bytes()

    try:
        _git("reset", "--hard", f"origin/{branch}")
        _assert_workdir_parses()
    except Exception:
        try:
            _git("reset", "--hard", before)
        except RuntimeError:
            pass
        for rel, data in preserved.items():
            try:
                (PROJECT_ROOT / rel).write_bytes(data)
            except OSError:
                pass
        raise

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
            _fetch_branch(branch)
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

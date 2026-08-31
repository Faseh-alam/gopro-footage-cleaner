"""Native folder picker for local Review Station use (Finder / Explorer)."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PickResult:
    path: Path | None = None
    cancelled: bool = False
    error: str | None = None


def pick_folder(initial: Path | None = None) -> Path | None:
    return pick_folder_result(initial).path


def pick_folder_result(initial: Path | None = None) -> PickResult:
    if initial is not None:
        initial = initial.expanduser()
        if not initial.exists():
            initial = None

    if sys.platform == "darwin":
        return _pick_folder_mac(initial)
    if sys.platform == "win32":
        return _result_from_path(_pick_folder_tk(initial))
    return _result_from_path(_pick_folder_tk(initial))


def _result_from_path(path: Path | None) -> PickResult:
    if path is None:
        return PickResult(cancelled=True)
    return PickResult(path=path)


def _normalize_chosen(raw: str) -> Path | None:
    text = (raw or "").strip().strip('"').strip("'").rstrip("/\\")
    if not text:
        return None
    path = Path(text).expanduser()
    try:
        path = path.resolve()
    except OSError:
        return None
    if not path.is_dir():
        return None
    return path


def _pick_folder_mac(initial: Path | None) -> PickResult:
    """Finder dialog first, then Tk. Never use System Events (needs extra Mac permission
    and makes the picker look like it did nothing)."""
    osa = _pick_folder_mac_osascript(initial)
    if osa.path is not None:
        return osa
    if osa.cancelled and not osa.error:
        return osa

    tk_path = _pick_folder_tk(initial)
    if tk_path is not None:
        return PickResult(path=tk_path)
    if osa.error:
        return PickResult(cancelled=True, error=osa.error)
    return PickResult(cancelled=True)


def _pick_folder_mac_osascript(initial: Path | None) -> PickResult:
    if initial is not None:
        location = str(initial.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        choose = (
            'choose folder with prompt "Select the 50-hour PARENT folder '
            '(contains the task folders with GX videos)" '
            f'default location (POSIX file "{location}")'
        )
    else:
        choose = (
            'choose folder with prompt "Select the 50-hour PARENT folder '
            '(contains the task folders with GX videos)"'
        )
    # Do not target System Events / Finder — those Apple Events are often blocked
    # for a background Flask process, so the dialog never appears.
    result = subprocess.run(
        ["osascript", "-e", f"POSIX path of ({choose})"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    err = (result.stderr or "").strip()
    if result.returncode != 0:
        lowered = err.lower()
        if (
            "-128" in err
            or "user canceled" in lowered
            or "user cancelled" in lowered
        ):
            return PickResult(cancelled=True)
        return PickResult(
            cancelled=True,
            error=err or "Finder could not open a folder picker from this app",
        )
    chosen = _normalize_chosen(result.stdout)
    if chosen is None:
        return PickResult(cancelled=True)
    return PickResult(path=chosen)


def _pick_folder_windows(initial: Path | None) -> Path | None:
    return _pick_folder_tk(initial)


def _pick_folder_tk(initial: Path | None) -> Path | None:
    initial_dir = str(initial.resolve()) if initial is not None else ""
    script = r"""
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.title("Select 50-hour folder")
root.geometry("420x80")
try:
    root.lift()
    root.attributes("-topmost", True)
    root.focus_force()
except tk.TclError:
    pass

start = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
path = filedialog.askdirectory(
    parent=root,
    title="Select the 50-hour parent folder",
    initialdir=start or None,
    mustexist=True,
)
print(path or "", end="")
try:
    root.destroy()
except tk.TclError:
    pass
"""
    executable = sys.executable
    if sys.platform == "win32" and executable.lower().endswith("python.exe"):
        pythonw = Path(executable).with_name("pythonw.exe")
        if pythonw.exists():
            executable = str(pythonw)

    args = [executable, "-c", script]
    if initial_dir:
        args.append(initial_dir)

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    return _normalize_chosen(result.stdout)

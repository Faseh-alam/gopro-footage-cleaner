"""Native folder picker for local Review Station use (Finder / Explorer)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def pick_folder(initial: Path | None = None) -> Path | None:
    if initial is not None:
        initial = initial.expanduser()
        if not initial.exists():
            initial = None

    if sys.platform == "darwin":
        return _pick_folder_mac(initial)
    if sys.platform == "win32":
        return _pick_folder_windows(initial)
    return _pick_folder_tk(initial)


def _pick_folder_mac(initial: Path | None) -> Path | None:
    if initial is not None:
        location = str(initial.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'POSIX path of (choose folder with prompt "Select footage folder" '
            f'default location (POSIX file "{location}"))'
        )
    else:
        script = 'POSIX path of (choose folder with prompt "Select footage folder")'

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _pick_folder_windows(initial: Path | None) -> Path | None:
    return _pick_folder_tk(initial)


def _pick_folder_tk(initial: Path | None) -> Path | None:
    initial_dir = str(initial.resolve()) if initial is not None else ""
    script = """
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
try:
    root.wm_attributes("-topmost", 1)
except tk.TclError:
    pass

start = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
path = filedialog.askdirectory(title="Select footage folder", initialdir=start)
print(path or "", end="")
root.destroy()
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

    raw = result.stdout.strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()

"""Download a local ffmpeg/ffprobe on first run if they are not installed."""

from __future__ import annotations

import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
FFMPEG_HOME = TOOLS / "ffmpeg"

DOWNLOADS = [
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
]


def _local_bin() -> Path | None:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    probe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    if FFMPEG_HOME.is_dir():
        for folder in [FFMPEG_HOME / "bin", *FFMPEG_HOME.rglob("bin")]:
            if (folder / exe).is_file() and (folder / probe).is_file():
                return folder
    return None


def _prepend_path(folder: Path) -> None:
    os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


def _have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading ffmpeg from {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "voiceover-player/1.0"})
    with urllib.request.urlopen(req, timeout=120) as response:
        total = int(response.headers.get("Content-Length") or 0)
        got = 0
        with dest.open("wb") as out:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if total:
                    pct = min(100, got * 100 // total)
                    sys.stdout.write(f"\r  {pct}% ({got / (1024 * 1024):.0f} MB)")
                    sys.stdout.flush()
    print()


def _extract(archive: Path) -> None:
    if FFMPEG_HOME.exists():
        shutil.rmtree(FFMPEG_HOME, ignore_errors=True)
    FFMPEG_HOME.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(FFMPEG_HOME)


def ensure_ffmpeg() -> bool:
    """Return True if ffmpeg and ffprobe are available."""
    if _have_ffmpeg():
        return True
    local = _local_bin()
    if local:
        _prepend_path(local)
        return _have_ffmpeg()
    if os.name != "nt":
        print("Install ffmpeg with your package manager (e.g. sudo apt install ffmpeg).")
        return False

    print("ffmpeg not found. Downloading it for this app (first run only)...")
    TOOLS.mkdir(parents=True, exist_ok=True)
    archive = TOOLS / "ffmpeg-download.zip"
    last_error = None
    for url in DOWNLOADS:
        try:
            _download(url, archive)
            _extract(archive)
            archive.unlink(missing_ok=True)
            local = _local_bin()
            if not local:
                raise RuntimeError("Downloaded ffmpeg zip, but ffmpeg.exe was not inside it.")
            _prepend_path(local)
            if _have_ffmpeg():
                print(f"ffmpeg ready at {local}")
                return True
        except Exception as exc:
            last_error = exc
            print(f"Download failed: {exc}")
            continue
    print("Could not install ffmpeg automatically. Export will not work until ffmpeg is available.")
    if last_error:
        print(f"Last error: {last_error}")
    return False

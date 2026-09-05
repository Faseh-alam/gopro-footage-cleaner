"""Voiceover station — scan USB class folders and mux narration into MP4s.

Preserves video + GoPro GPMF/IMU via stream-copy; replaces the audio track
with the student narration. Temp files only — final output is the same path.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .ffmpeg_tools import ffmpeg_bin
from .probe import MediaInfo, is_video_file, probe_media

PROGRESS_NAME = "_voiceover_progress.json"
_lock = threading.Lock()


@dataclass
class VoiceoverClip:
    path: Path
    name: str
    class_name: str
    duration: float | None
    size_bytes: int
    has_gpmf: bool
    done: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def resolve_voiceover_root(path: Path) -> Path:
    """Prefer a child named ``voiceover`` when the user picks the USB root."""
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Not a folder: {root}")
    if root.name.lower() == "voiceover":
        return root
    child = root / "voiceover"
    if child.is_dir():
        return child.resolve()
    return root


def progress_path(root: Path) -> Path:
    return resolve_voiceover_root(root) / PROGRESS_NAME


def load_progress(root: Path) -> dict:
    path = progress_path(root)
    if not path.is_file():
        return {"version": 1, "done": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "done": {}}
    if not isinstance(data, dict):
        return {"version": 1, "done": {}}
    done = data.get("done")
    if not isinstance(done, dict):
        data["done"] = {}
    return data


def mark_done(root: Path, video: Path, *, narrator: str = "", mic: str = "") -> None:
    root = resolve_voiceover_root(root)
    video = video.expanduser().resolve()
    with _lock:
        data = load_progress(root)
        data.setdefault("done", {})[str(video)] = {
            "at": _now_iso(),
            "narrator": narrator,
            "mic": mic,
            "size_bytes": video.stat().st_size if video.is_file() else None,
        }
        data["updated_at"] = _now_iso()
        progress_path(root).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _is_junk(path: Path) -> bool:
    name = path.name
    lower = name.lower()
    if name.startswith("."):
        return True
    if name.startswith("._"):
        return True
    if ".partial" in lower:
        return True
    if lower == PROGRESS_NAME:
        return True
    return False


def scan_voiceover_tree(root: Path) -> dict:
    """Scan ``voiceover/<class>/**/*.mp4`` (or a class folder directly)."""
    root = resolve_voiceover_root(root)
    progress = load_progress(root)
    done_map: dict = progress.get("done") or {}

    classes: dict[str, list[VoiceoverClip]] = {}

    # If root itself contains videos, treat as a single class = root name.
    direct_videos = [
        p
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower())
        if p.is_file() and is_video_file(p) and not _is_junk(p)
    ]
    class_dirs = [
        p
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower())
        if p.is_dir() and not p.name.startswith(".") and p.name != PROGRESS_NAME
    ]

    def add_clip(class_name: str, video: Path) -> None:
        try:
            media = probe_media(video)
        except Exception:  # noqa: BLE001
            media = None
        key = str(video.resolve())
        clip = VoiceoverClip(
            path=video.resolve(),
            name=video.name,
            class_name=class_name,
            duration=media.duration if media else None,
            size_bytes=video.stat().st_size,
            has_gpmf=bool(media and media.has_gpmf),
            done=key in done_map,
        )
        classes.setdefault(class_name, []).append(clip)

    if direct_videos and not class_dirs:
        for video in direct_videos:
            add_clip(root.name, video)
    else:
        for class_dir in class_dirs:
            for video in sorted(class_dir.rglob("*"), key=lambda p: str(p).lower()):
                if not video.is_file() or not is_video_file(video) or _is_junk(video):
                    continue
                add_clip(class_dir.name, video)

    class_rows = []
    total = 0
    done_count = 0
    for name in sorted(classes.keys(), key=str.lower):
        clips = classes[name]
        total += len(clips)
        done_count += sum(1 for c in clips if c.done)
        class_rows.append(
            {
                "name": name,
                "clip_count": len(clips),
                "done_count": sum(1 for c in clips if c.done),
                "clips": [
                    {
                        "path": str(c.path),
                        "name": c.name,
                        "class_name": c.class_name,
                        "duration": c.duration,
                        "size_bytes": c.size_bytes,
                        "has_gpmf": c.has_gpmf,
                        "done": c.done,
                    }
                    for c in clips
                ],
            }
        )

    return {
        "root": str(root),
        "classes": class_rows,
        "clip_count": total,
        "done_count": done_count,
    }


def _build_mux_command(video: MediaInfo, audio_path: Path, output: Path) -> list[str]:
    if video.video_index is None:
        raise RuntimeError(f"No video stream: {video.path.name}")

    # Explicit filter_complex so we never accidentally keep (or mix) the original
    # GoPro audio — only the uploaded narration becomes the AAC track.
    duration = float(video.duration or 0)
    if duration > 0:
        audio_filter = (
            f"[1:a:0]aformat=sample_rates=48000:channel_layouts=mono,"
            f"apad=whole_dur={duration:.6f},atrim=0:{duration:.6f}[narration]"
        )
    else:
        audio_filter = (
            "[1:a:0]aformat=sample_rates=48000:channel_layouts=mono[narration]"
        )

    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(video.path),
        "-i",
        str(audio_path),
        "-filter_complex",
        audio_filter,
        "-map",
        f"0:{video.video_index}",
        "-map",
        "[narration]",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "1",
    ]

    if duration > 0:
        command.extend(["-t", f"{duration:.6f}"])

    if video.gpmf_index is not None:
        # After remap: 0=video, 1=audio, 2=data → tag data as gpmd.
        command.extend(
            [
                "-map",
                f"0:{video.gpmf_index}",
                "-copy_unknown",
                "-c:d",
                "copy",
                "-tag:d:0",
                "gpmd",
            ]
        )

    command.extend(["-movflags", "+faststart", "-f", "mp4", str(output)])
    return command


def _assert_narration_usable(audio_path: Path) -> None:
    """Reject empty / near-empty takes before touching the source MP4."""
    size = audio_path.stat().st_size if audio_path.is_file() else 0
    if size < 512:
        raise RuntimeError(
            f"Recorded audio is empty or too small ({size} bytes) — try again"
        )
    try:
        media = probe_media(audio_path)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Could not read recorded audio: {exc}") from exc
    if media.audio_index is None:
        raise RuntimeError("Recorded file has no audio stream — try again")
    dur = float(media.duration or 0)
    if 0 < dur < 0.35:
        raise RuntimeError(
            f"Take too short ({dur:.2f}s) — hold R longer while speaking"
        )


def mux_voiceover_inplace(
    source: Path,
    audio_path: Path,
    *,
    root: Path | None = None,
    narrator: str = "",
    mic: str = "",
) -> dict:
    """Replace audio on ``source`` in place; preserve video + GPMF. Fail closed."""
    source = source.expanduser().resolve(strict=True)
    audio_path = audio_path.expanduser().resolve(strict=True)
    if not is_video_file(source):
        raise ValueError(f"Not a video file: {source.name}")
    if source.suffix.lower() not in {".mp4", ".mov"}:
        raise ValueError("Voiceover mux currently supports MP4/MOV only")

    media = probe_media(source)
    had_gpmf = media.has_gpmf
    _assert_narration_usable(audio_path)

    ext = source.suffix or ".MP4"
    partial = source.with_name(f"{source.stem}.partial{ext}")
    if partial.exists():
        partial.unlink()

    command = _build_mux_command(media, audio_path, partial)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        if partial.exists():
            partial.unlink(missing_ok=True)
        err = (result.stderr or result.stdout or "ffmpeg mux failed").strip()
        raise RuntimeError(err)

    try:
        out_media = probe_media(partial)
    except Exception as exc:  # noqa: BLE001
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Could not probe muxed output: {exc}") from exc

    if had_gpmf and not out_media.has_gpmf:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            "Muxed file is missing GPMF / IMU — original left untouched"
        )

    # Atomic-ish replace on same volume.
    backup = source.with_name(f"{source.stem}.__pre_voiceover__{ext}")
    try:
        if backup.exists():
            backup.unlink()
        os.replace(source, backup)
        os.replace(partial, source)
        backup.unlink(missing_ok=True)
    except OSError:
        # Roll back if replace failed mid-way.
        if not source.exists() and backup.exists():
            os.replace(backup, source)
        partial.unlink(missing_ok=True)
        raise

    if root is not None:
        try:
            mark_done(root, source, narrator=narrator, mic=mic)
        except OSError:
            pass

    final = probe_media(source)
    return {
        "ok": True,
        "path": str(source),
        "has_gpmf": final.has_gpmf,
        "duration": final.duration,
        "size_bytes": final.size_bytes,
        "message": f"Rewrote original clip: {source}"
        + (" · GPMF preserved" if final.has_gpmf else ""),
    }


def save_uploaded_audio(upload_bytes: bytes, suffix: str = ".webm") -> Path:
    """Write uploaded browser audio to a temp file (caller deletes)."""
    if suffix.lower() not in {".webm", ".ogg", ".wav", ".mp4", ".m4a", ".mp3"}:
        suffix = ".webm"
    fd, name = tempfile.mkstemp(prefix="voiceover-", suffix=suffix)
    os.close(fd)
    path = Path(name)
    path.write_bytes(upload_bytes)
    return path


def build_gemini_proxy(
    source: Path,
    *,
    start: float = 0.0,
    end: float | None = None,
    max_width: int = 640,
    fps: int = 4,
) -> Path:
    """Build a tiny silent MP4 proxy for Gemini (never upload the full GoPro file)."""
    source = source.expanduser().resolve(strict=True)
    media = probe_media(source)
    duration = float(media.duration or 0)
    start = max(0.0, float(start))
    if end is None or end <= start:
        end = duration if duration > 0 else start + 60.0
    end = float(end)
    if duration > 0:
        end = min(end, duration)
    length = max(0.5, end - start)

    fd, name = tempfile.mkstemp(prefix="gemini-proxy-", suffix=".mp4")
    os.close(fd)
    out = Path(name)
    if out.exists():
        out.unlink()

    # Low-res, low-fps, no audio — keeps uploads small even for long clips.
    vf = f"fps={max(1, int(fps))},scale={int(max_width)}:-2"
    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{length:.3f}",
        "-i",
        str(source),
        "-an",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "32",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or not out.is_file() or out.stat().st_size < 200:
        out.unlink(missing_ok=True)
        err = (result.stderr or result.stdout or "proxy encode failed").strip()
        raise RuntimeError(f"Could not build Gemini proxy: {err[:400]}")
    return out

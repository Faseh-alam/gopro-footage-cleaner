"""Trim mapped segments on SSD after SD→SSD copy (no live trim on card).

Reads ``*.segments.json`` beside source MP4s and writes clips into
``{dest}/{task-slug}/{stem}-{n}.MP4`` using ffmpeg stream copy when possible.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from pathlib import Path

_lock = threading.Lock()
_jobs: dict[str, dict] = {}

_SLUG_RE = re.compile(r"[^\w\s-]", re.UNICODE)


def task_slug(name: str) -> str:
    slug = _SLUG_RE.sub("", (name or "").strip().lower())
    return re.sub(r"[-\s]+", "-", slug).strip("-") or "task"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ffmpeg() -> str:
    # Prefer gopro_cleaner ffmpeg helper when available.
    root = _repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from gopro_cleaner.core.ffmpeg_tools import ffmpeg_bin  # type: ignore

        return ffmpeg_bin()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _format_ts(seconds: float) -> str:
    total = max(0.0, float(seconds))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def load_segments_file(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    segs = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(segs, list):
        return []
    out = []
    for raw in segs:
        if not isinstance(raw, dict):
            continue
        try:
            start = float(raw.get("start", 0))
            end = float(raw.get("end", 0))
        except (TypeError, ValueError):
            continue
        task = str(raw.get("task") or "").strip()
        if end <= start + 0.001 or not task:
            continue
        out.append({"start": start, "end": end, "task": task})
    return out


def find_segment_jobs(dest_root: Path) -> list[dict]:
    """Return trim jobs for every MP4 under dest that has a segments sidecar."""
    jobs: list[dict] = []
    if not dest_root.is_dir():
        return jobs
    for json_path in sorted(dest_root.rglob("*.segments.json")):
        stem = json_path.name[: -len(".segments.json")]
        video = json_path.with_name(f"{stem}.MP4")
        if not video.is_file():
            video = json_path.with_name(f"{stem}.mp4")
        if not video.is_file():
            continue
        segments = load_segments_file(json_path)
        if not segments:
            continue
        jobs.append({"video": video, "segments": segments, "sidecar": json_path})
    return jobs


def _next_clip_number(output_dir: Path, stem: str) -> int:
    n = 1
    while (output_dir / f"{stem}-{n}.MP4").exists() or (output_dir / f"{stem}-{n}.mp4").exists():
        n += 1
    return n


def _trim_one(video: Path, start: float, end: float, output: Path) -> None:
    duration = max(0.05, end - start)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        _format_ts(start),
        "-i",
        str(video),
        "-t",
        f"{duration:.3f}",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip()
        raise RuntimeError(detail[-400:])


def trim_destination(dest_root: Path, *, card_id: str = "") -> dict:
    """Trim all mapped segments under an SSD card destination folder."""
    dest_root = Path(dest_root)
    jobs = find_segment_jobs(dest_root)
    made = 0
    errors: list[str] = []
    for job in jobs:
        video: Path = job["video"]
        stem = video.stem
        # Prefer base stem without prior -N suffix
        base = re.sub(r"-\d+$", "", stem) or stem
        for seg in job["segments"]:
            slug = task_slug(seg["task"])
            out_dir = dest_root / slug
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                n = _next_clip_number(out_dir, base)
                out = out_dir / f"{base}-{n}{video.suffix or '.MP4'}"
                _trim_one(video, seg["start"], seg["end"], out)
                made += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{video.name} [{seg['start']}-{seg['end']}] {exc}")
    summary = {
        "card_id": card_id,
        "dest": str(dest_root),
        "sources": len(jobs),
        "clips_made": made,
        "errors": errors,
        "ok": not errors,
    }
    with _lock:
        _jobs[card_id or str(dest_root)] = {
            **summary,
            "status": "completed" if not errors else "error",
            "message": (
                f"Auto-trim done · {made} clip(s)"
                if not errors
                else f"Auto-trim finished with {len(errors)} error(s) · {made} clip(s) ok"
            ),
        }
    return summary


def start_trim_async(dest_root: Path, *, card_id: str = "", on_done=None) -> None:
    def _run() -> None:
        try:
            result = trim_destination(dest_root, card_id=card_id)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "errors": [str(exc)], "clips_made": 0, "card_id": card_id}
        if on_done:
            try:
                on_done(result)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_run, daemon=True, name=f"segment-trim-{card_id or 'job'}").start()


def list_trim_jobs() -> list[dict]:
    with _lock:
        return [dict(v) for v in _jobs.values()]

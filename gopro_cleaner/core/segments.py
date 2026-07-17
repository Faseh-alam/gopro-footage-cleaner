"""Sidecar segment maps next to source MP4s (no live trim on card).

Files:
  GX01.MP4  →  GX01.segments.json  (machine)
            →  GX01.segments.txt   (human-readable)
"""

from __future__ import annotations

import json
from pathlib import Path

from .timestamps import format_timestamp

SEGMENTS_VERSION = 1


def segments_json_path(video: Path) -> Path:
    return video.with_name(f"{video.stem}.segments.json")


def segments_txt_path(video: Path) -> Path:
    return video.with_name(f"{video.stem}.segments.txt")


def load_segments(video: Path) -> dict:
    path = segments_json_path(video)
    if not path.is_file():
        return {
            "version": SEGMENTS_VERSION,
            "source": video.name,
            "segments": [],
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "version": SEGMENTS_VERSION,
            "source": video.name,
            "segments": [],
        }
    if not isinstance(data, dict):
        data = {}
    segs = data.get("segments") or []
    if not isinstance(segs, list):
        segs = []
    return {
        "version": int(data.get("version") or SEGMENTS_VERSION),
        "source": str(data.get("source") or video.name),
        "segments": segs,
    }


def save_segments(video: Path, segments: list[dict]) -> dict:
    """Write JSON + TXT sidecars beside the video. Returns the saved payload."""
    video = video.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(f"Video not found: {video}")

    cleaned: list[dict] = []
    for raw in segments:
        start = float(raw.get("start", 0))
        end = float(raw.get("end", 0))
        task = str(raw.get("task") or "").strip()
        if end <= start + 0.001 or not task:
            continue
        cleaned.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "task": task,
                "shortcut": str(raw.get("shortcut") or "").strip().lower()[:1] or None,
            }
        )
    cleaned.sort(key=lambda s: (s["start"], s["end"]))

    payload = {
        "version": SEGMENTS_VERSION,
        "source": video.name,
        "segments": cleaned,
    }
    json_path = segments_json_path(video)
    txt_path = segments_txt_path(video)
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# Segments for {video.name}",
        "# start_end  task",
        "",
    ]
    for seg in cleaned:
        lines.append(
            f"{format_timestamp(seg['start'])}-{format_timestamp(seg['end'])}  {seg['task']}"
        )
    if not cleaned:
        lines.append("# (no segments yet)")
    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def list_segment_sidecars(root: Path) -> list[Path]:
    """Find all *.segments.json under root."""
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.segments.json"))

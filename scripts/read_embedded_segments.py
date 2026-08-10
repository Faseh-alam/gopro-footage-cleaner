#!/usr/bin/env python3
"""Read the segments metadata embedded in offloaded GoPro MP4s.

The SD Offloader appends each video's GoPro Cleaner ``.segments.json`` payload
into the MP4 itself (a spec-compliant top-level ``skip`` box tagged ``WCSG``).
This standalone reader is the building block for the AWS-side pipeline: point
it at a batch folder downloaded from S3 and it tells you, per video, which
task segments to cut — no sidecar files required (though it falls back to
``<name>.segments.json`` when present).

Usage:
    python scripts/read_embedded_segments.py VIDEO.MP4 [more.mp4 | folder ...]
    python scripts/read_embedded_segments.py --json VIDEO.MP4     # full payload
    python scripts/read_embedded_segments.py --ffmpeg BATCH_DIR   # print cut commands

Example downstream cut (stream copy, IMU/gpmd preserved):
    ffmpeg -ss 12.4 -to 88.2 -i GX010001.MP4 -map 0 -c copy -ignore_unknown out.mp4
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

MAGIC = b"WCSG"
_HEADER = 8

# Windows consoles default to cp1252 — don't crash on non-ASCII task names.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def read_embedded_segments(mp4: Path) -> dict | None:
    """Return the embedded payload (last WCSG ``skip`` box), or None."""
    file_size = mp4.stat().st_size
    found: tuple[int, int] | None = None
    with mp4.open("rb") as handle:
        offset = 0
        while offset + _HEADER <= file_size:
            handle.seek(offset)
            header = handle.read(_HEADER)
            if len(header) < _HEADER:
                break
            size = struct.unpack(">I", header[:4])[0]
            fourcc = header[4:8]
            if size == 1:
                big = handle.read(8)
                if len(big) < 8:
                    break
                size = struct.unpack(">Q", big)[0]
            elif size == 0:
                size = file_size - offset
            if size < _HEADER or offset + size > file_size:
                break
            if fourcc == b"skip" and size >= _HEADER + len(MAGIC):
                handle.seek(offset + _HEADER)
                if handle.read(len(MAGIC)) == MAGIC:
                    found = (offset, size)
            offset += size
        if not found:
            return None
        box_offset, box_size = found
        handle.seek(box_offset + _HEADER + len(MAGIC) + 4)  # magic + version
        raw = handle.read(box_size - _HEADER - len(MAGIC) - 4)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_payload(mp4: Path) -> tuple[dict | None, str]:
    """Embedded payload first, sidecar JSON as fallback."""
    payload = read_embedded_segments(mp4)
    if payload is not None:
        return payload, "embedded"
    sidecar = mp4.with_name(f"{mp4.stem}.segments.json")
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data, "sidecar"
        except (json.JSONDecodeError, OSError):
            pass
    return None, "none"


def collect_mp4s(targets: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in targets:
        path = Path(raw)
        if path.is_dir():
            out.extend(sorted(p for p in path.rglob("*") if p.suffix.upper() == ".MP4"))
        elif path.is_file():
            out.append(path)
        else:
            print(f"!! not found: {path}", file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("targets", nargs="+", help="MP4 files and/or folders (recursive)")
    parser.add_argument("--json", action="store_true", help="dump the full payload as JSON")
    parser.add_argument(
        "--ffmpeg",
        action="store_true",
        help="print ffmpeg stream-copy commands for every work segment",
    )
    args = parser.parse_args()

    videos = collect_mp4s(args.targets)
    if not videos:
        print("No MP4 files found.", file=sys.stderr)
        return 1

    missing = 0
    for mp4 in videos:
        payload, origin = load_payload(mp4)
        if payload is None:
            missing += 1
            print(f"\n{mp4}\n  (no embedded segments, no sidecar)")
            continue

        if args.json:
            print(json.dumps({"file": str(mp4), "origin": origin, "payload": payload}, indent=2))
            continue

        meta = payload.get("media_meta") or {}
        segments = payload.get("segments") or []
        work = [s for s in segments if s.get("kind") == "work"]
        print(f"\n{mp4}  [{origin}]")
        print(
            f"  batch={payload.get('batch_name')!r} card={payload.get('card_badge')!r} "
            f"complete={payload.get('complete')} duration={payload.get('duration')}"
        )
        if meta:
            print(
                f"  recorded={meta.get('recorded_at') or '?'} "
                f"camera={meta.get('camera_model') or '?'} SN={meta.get('camera_serial') or '?'}"
            )
        for seg in segments:
            kind = seg.get("kind")
            task = seg.get("task") or ""
            print(
                f"    {kind:<8} {float(seg.get('start') or 0):>9.2f} → "
                f"{float(seg.get('end') or 0):>9.2f}  {task}"
            )
        if args.ffmpeg:
            for idx, seg in enumerate(work, 1):
                task = (seg.get("task") or "task").strip().replace(" ", "-").lower()
                out = mp4.with_name(f"{mp4.stem}_{idx:02d}_{task}.mp4")
                print(
                    f"  ffmpeg -ss {float(seg.get('start') or 0):.3f} "
                    f"-to {float(seg.get('end') or 0):.3f} -i \"{mp4}\" "
                    f"-map 0 -c copy -ignore_unknown \"{out}\""
                )

    print(f"\n{len(videos)} video(s), {len(videos) - missing} with segments metadata.")
    return 0 if missing == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

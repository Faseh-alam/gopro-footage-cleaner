#!/usr/bin/env python3
"""Rebuild trimmed work footage from an offloaded batch — made for the AWS server.

Feed it a batch folder (synced down from S3 by the SD Offloader). For every
video it reads the labeling data embedded inside the MP4 by the offloader
(WCSG ``skip`` box; falls back to the ``<name>.segments.json`` sidecar), then
cuts ONLY the ``work`` segments into task-name folders:

    <output>/
      pipe-welding/
        GX010001_01.mp4        ← trimmed work clip (video + audio + GPMF/IMU)
      cable-pulling/
        GX010002_01.mp4
        GX010002_02.mp4

Cutting is a stream copy with the same mapping as GoPro Cleaner's local
trimmer (video + audio + gpmd data track, ``-copy_unknown -tag:d gpmd``),
so no re-encode and the IMU data survives in every clip. Each clip also gets
its own metadata embedded (source file, task, start/end, camera serial,
device id...), readable with scripts/read_embedded_segments.py.

Only needs Python 3.9+ and ffmpeg/ffprobe on PATH (or set FFMPEG/FFPROBE).

Usage:
    python aws_trim_batch.py "/data/batch 6"                       # → /data/batch 6/_trimmed
    python aws_trim_batch.py "/data/batch 6" --output /data/out
    python aws_trim_batch.py "/data/batch 6" --dry-run             # plan only
    python aws_trim_batch.py "/data/batch 6" --include-incomplete  # also not-fully-labeled videos
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

MAGIC = b"WCSG"
_HEADER = 8

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- embedded IO

def _walk_top_level(handle, file_size: int):
    offset = 0
    while offset + _HEADER <= file_size:
        handle.seek(offset)
        header = handle.read(_HEADER)
        if len(header) < _HEADER:
            return
        size = struct.unpack(">I", header[:4])[0]
        fourcc = header[4:8]
        if size == 1:
            big = handle.read(8)
            if len(big) < 8:
                return
            size = struct.unpack(">Q", big)[0]
        elif size == 0:
            size = file_size - offset
        if size < _HEADER or offset + size > file_size:
            return
        yield offset, size, fourcc
        offset += size


def read_embedded_segments(mp4: Path) -> dict | None:
    file_size = mp4.stat().st_size
    found = None
    with mp4.open("rb") as handle:
        for offset, size, fourcc in _walk_top_level(handle, file_size):
            if fourcc == b"skip" and size >= _HEADER + len(MAGIC):
                handle.seek(offset + _HEADER)
                if handle.read(len(MAGIC)) == MAGIC:
                    found = (offset, size)
        if not found:
            return None
        offset, size = found
        handle.seek(offset + _HEADER + len(MAGIC) + 4)
        raw = handle.read(size - _HEADER - len(MAGIC) - 4)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def embed_payload(mp4: Path, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body = MAGIC + struct.pack(">I", 1) + data
    box = struct.pack(">I", _HEADER + len(body)) + b"skip" + body
    with mp4.open("ab") as handle:
        handle.write(box)


def load_payload(mp4: Path) -> tuple[dict | None, str]:
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


# ------------------------------------------------------------------- ffmpeg

def _bin(name: str) -> str:
    env = os.environ.get(name.upper())
    if env and Path(env).is_file():
        return env
    found = shutil.which(name)
    if not found:
        sys.exit(f"error: {name} not found — install ffmpeg or set {name.upper()}=/path/to/{name}")
    return found


def probe_streams(ffprobe: str, mp4: Path) -> dict:
    """Return {'video': idx|None, 'audio': idx|None, 'gpmf': idx|None}."""
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(mp4)],
        capture_output=True, text=True, timeout=120,
    )
    out = {"video": None, "audio": None, "gpmf": None}
    if result.returncode != 0:
        return out
    try:
        streams = json.loads(result.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        return out
    for stream in streams:
        idx = stream.get("index")
        kind = stream.get("codec_type")
        if kind == "video" and out["video"] is None:
            out["video"] = idx
        elif kind == "audio" and out["audio"] is None:
            out["audio"] = idx
        elif kind == "data" and out["gpmf"] is None:
            tag = str(stream.get("codec_tag_string") or "")
            handler = str((stream.get("tags") or {}).get("handler_name") or "")
            if "gpmd" in tag.lower() or "gopro met" in handler.lower():
                out["gpmf"] = idx
    return out


def build_cut_command(
    ffmpeg: str, src: Path, out: Path, start: float, duration: float, streams: dict
) -> list[str]:
    """Same recipe as GoPro Cleaner's trimmer: stream copy, gpmd kept."""
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
    ]
    if streams["video"] is not None:
        command += ["-map", f"0:{streams['video']}"]
    if streams["audio"] is not None:
        command += ["-map", f"0:{streams['audio']}"]
    if streams["gpmf"] is not None:
        data_tag_index = 2 if streams["audio"] is not None else 1
        command += [
            "-map", f"0:{streams['gpmf']}",
            "-copy_unknown", f"-tag:d:{data_tag_index}", "gpmd",
        ]
    if streams["video"] is None and streams["audio"] is None:
        command += ["-map", "0"]  # last resort: take everything
    command += ["-avoid_negative_ts", "make_zero", "-c", "copy", str(out)]
    return command


def try_udtacopy(src: Path, out: Path) -> None:
    """Optionally restore GoPro udta boxes (firmware/serial atoms) into the clip."""
    tool = shutil.which("udtacopy")
    if not tool:
        return
    try:
        subprocess.run([tool, str(src), str(out)], capture_output=True, timeout=300)
    except Exception:  # noqa: BLE001 — cosmetic step, never fail the cut
        pass


# -------------------------------------------------------------------- naming

def task_slug(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", str(name).strip().lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug or "unknown-task"


# ---------------------------------------------------------------------- main

def collect_mp4s(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.upper() == ".MP4" and not p.name.startswith("._")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Cut work-task clips from an offloaded batch")
    parser.add_argument("batch", help="batch folder downloaded from S3")
    parser.add_argument("--output", help="output root (default: <batch>/_trimmed)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, cut nothing")
    parser.add_argument(
        "--include-incomplete", action="store_true",
        help="also process videos whose labeling is not marked complete",
    )
    args = parser.parse_args()

    batch = Path(args.batch).expanduser().resolve()
    if not batch.is_dir():
        sys.exit(f"error: batch folder not found: {batch}")
    out_root = Path(args.output).expanduser().resolve() if args.output else batch / "_trimmed"

    ffmpeg = _bin("ffmpeg") if not args.dry_run else ""
    ffprobe = _bin("ffprobe") if not args.dry_run else ""

    videos = collect_mp4s(batch)
    if not videos:
        sys.exit(f"error: no MP4 files under {batch}")

    clips_done = 0
    clips_failed = 0
    skipped_unlabeled: list[str] = []
    skipped_incomplete: list[str] = []

    for mp4 in videos:
        if out_root in mp4.parents:
            continue  # never re-process our own output
        payload, origin = load_payload(mp4)
        if payload is None:
            skipped_unlabeled.append(mp4.name)
            continue
        if payload.get("complete") is not True and not args.include_incomplete:
            skipped_incomplete.append(mp4.name)
            continue

        work = [
            seg for seg in payload.get("segments") or []
            if isinstance(seg, dict) and seg.get("kind") == "work"
        ]
        if not work:
            continue

        meta = payload.get("media_meta") or {}
        print(f"\n{mp4.name}  [{origin}]  {len(work)} work segment(s)")
        streams = probe_streams(ffprobe, mp4) if not args.dry_run else {}

        for idx, seg in enumerate(work, 1):
            start = float(seg.get("start") or 0.0)
            end = float(seg.get("end") or 0.0)
            duration = max(0.0, end - start)
            if duration <= 0:
                continue
            folder = out_root / task_slug(seg.get("task") or "")
            clip = folder / f"{mp4.stem}_{idx:02d}{mp4.suffix.lower()}"
            print(f"  {seg.get('task') or 'unknown'}: {start:.2f}s → {end:.2f}s  ⇒  {clip.relative_to(out_root)}")
            if args.dry_run:
                continue

            folder.mkdir(parents=True, exist_ok=True)
            command = build_cut_command(ffmpeg, mp4, clip, start, duration, streams)
            result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0 or not clip.is_file() or clip.stat().st_size == 0:
                clips_failed += 1
                print(f"    FAILED: {(result.stderr or '').strip()[:300]}")
                clip.unlink(missing_ok=True)
                continue

            try_udtacopy(mp4, clip)
            # Give the clip its own identity for downstream tooling.
            embed_payload(clip, {
                "version": 1,
                "clip_of": mp4.name,
                "task": seg.get("task") or "",
                "kind": "work",
                "start": start,
                "end": end,
                "batch_name": payload.get("batch_name") or "",
                "factory": payload.get("factory") or "",
                "card_badge": payload.get("card_badge") or "",
                "device_type": payload.get("device_type") or "",
                "device_id": payload.get("device_id") or "",
                "media_meta": meta,
            })
            clips_done += 1

    print(f"\n== {clips_done} clip(s) created under {out_root}")
    if skipped_incomplete:
        print(f"== skipped (labeling not complete): {', '.join(skipped_incomplete)}")
    if skipped_unlabeled:
        print(f"== skipped (no segments data): {', '.join(skipped_unlabeled)}")
    if clips_failed:
        print(f"== {clips_failed} clip(s) FAILED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

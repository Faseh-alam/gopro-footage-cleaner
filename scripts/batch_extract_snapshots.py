#!/usr/bin/env python3
"""Batch-extract filmstrip JPEGs for a folder of mixed-length clips.

Uses the same spacing formula as Eager Review:

  tolerance = duration * garbage_percent
  interval  = clamp(tolerance / N, MIN_INTERVAL, MAX_INTERVAL)
  timestamps = 0, interval, 2*interval, ...

Examples:
  python scripts/batch_extract_snapshots.py /path/to/clips
  python scripts/batch_extract_snapshots.py /path/to/clips --out /tmp/snaps
  python scripts/batch_extract_snapshots.py /path/to/clips --garbage 0.12 --N 3
  python scripts/batch_extract_snapshots.py /path/to/clips --dry-run

Works well for verifying 11 / 17 / 30 / 60 minute clips on a slow PC
(i5 8th gen / 16GB): seeks with -ss before -i and writes small JPEGs (-q:v 4).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gopro_cleaner.core.probe import probe_media  # noqa: E402
from gopro_cleaner.core.snapshot_strip import (  # noqa: E402
    compute_snapshot_interval,
    extract_snapshot_jpeg,
    get_snapshot_timestamps,
)
from gopro_cleaner.core.snapshot_settings import load_snapshot_settings  # noqa: E402

VIDEO_EXTS = {".mp4", ".MP4", ".mov", ".MOV", ".mkv", ".MKV"}


def _iter_videos(folder: Path, recursive: bool) -> list[Path]:
    if recursive:
        paths = [p for p in folder.rglob("*") if p.is_file() and p.suffix in VIDEO_EXTS]
    else:
        paths = [p for p in folder.iterdir() if p.is_file() and p.suffix in VIDEO_EXTS]
    return sorted(paths, key=lambda p: p.name.lower())


def _format_duration(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def process_clip(
    source: Path,
    out_root: Path,
    *,
    garbage_percent: float,
    N: int,
    dry_run: bool,
) -> dict:
    info = probe_media(source)
    duration = float(info.duration or 0.0)
    interval = compute_snapshot_interval(duration, garbage_percent=garbage_percent, N=N)
    times = get_snapshot_timestamps(duration, garbage_percent=garbage_percent, N=N)

    clip_out = out_root / source.stem
    result = {
        "file": str(source),
        "duration_sec": round(duration, 2),
        "duration_label": _format_duration(duration),
        "garbage_percent": garbage_percent,
        "N": N,
        "interval_sec": interval,
        "num_snapshots": len(times),
        "timestamps": times,
        "output_dir": str(clip_out),
        "extracted": 0,
        "failed": 0,
    }

    print(
        f"\n{source.name}  {_format_duration(duration)}  "
        f"interval={interval:.1f}s  snapshots={len(times)}"
    )
    print(f"  times: {times[:6]}{'...' if len(times) > 6 else ''}")

    if dry_run:
        return result

    clip_out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    for index, timestamp in enumerate(times):
        dest = clip_out / f"frame_{index:05d}_{timestamp:08.1f}s.jpg"
        try:
            extract_snapshot_jpeg(source, timestamp, dest)
            result["extracted"] += 1
            print(f"  [{index + 1}/{len(times)}] t={timestamp:.1f}s → {dest.name}")
        except RuntimeError as exc:
            result["failed"] += 1
            print(f"  [{index + 1}/{len(times)}] t={timestamp:.1f}s FAILED: {exc}")

    result["elapsed_sec"] = round(time.time() - started, 2)
    (clip_out / "manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    snap = load_snapshot_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Folder of clips (mixed lengths OK)")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output folder (default: <folder>/_snapshots)",
    )
    parser.add_argument(
        "--garbage",
        type=float,
        default=snap["garbage_percent"],
        help=f"garbage_percent (default {snap['garbage_percent']})",
    )
    parser.add_argument(
        "--N",
        type=int,
        default=snap["resolution_factor"],
        help=f"resolution_factor N (default {snap['resolution_factor']})",
    )
    parser.add_argument("--recursive", action="store_true", help="Scan subfolders")
    parser.add_argument("--dry-run", action="store_true", help="Print plan only, no ffmpeg")
    args = parser.parse_args()

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 1

    out_root = (args.out or (folder / "_snapshots")).expanduser().resolve()
    videos = _iter_videos(folder, recursive=args.recursive)
    if not videos:
        print(f"No videos found in {folder}", file=sys.stderr)
        return 1

    print(f"Found {len(videos)} clip(s) in {folder}")
    print(
        f"Config: garbage_percent={args.garbage}  N={args.N}  "
        f"MIN={snap['min_interval_sec']}s  MAX={snap['max_interval_sec']}s"
    )
    if not args.dry_run:
        out_root.mkdir(parents=True, exist_ok=True)

    summaries = []
    for video in videos:
        summaries.append(
            process_clip(
                video,
                out_root,
                garbage_percent=args.garbage,
                N=args.N,
                dry_run=args.dry_run,
            )
        )

    print("\n=== Summary ===")
    for row in summaries:
        print(
            f"{Path(row['file']).name:40s}  {row['duration_label']:>8s}  "
            f"every {row['interval_sec']:.0f}s  → {row['num_snapshots']} snaps"
            + ("" if args.dry_run else f"  ({row['extracted']} ok, {row['failed']} fail)")
        )

    if not args.dry_run:
        summary_path = out_root / "batch_summary.json"
        summary_path.write_text(json.dumps(summaries, indent=2), encoding="utf-8")
        print(f"\nWrote {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

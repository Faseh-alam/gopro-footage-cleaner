#!/usr/bin/env python3
"""Fast folder move/copy — same large-buffer approach as SD Offloader.

Cross-volume moves (SD → SSD) cannot rename; they copy with a 16 MB buffer,
verify size, then delete the source. Same-volume moves use an instant rename.

Examples:
    # Dry-run: see what would move
    python fast_move.py "E:\\DCIM\\100GOPRO" "D:\\temp\\parked" --dry-run

    # Move everything except one test clip (+ its sidecar)
    python fast_move.py "E:\\DCIM\\100GOPRO" "D:\\temp\\parked" --keep GH010062.MP4

    # Move only MP4s + sidecars matching a pattern
    python fast_move.py "E:\\DCIM\\100GOPRO" "D:\\temp\\parked" --only "*.MP4" --only "*.segments.json"

    # Copy only (leave originals)
    python fast_move.py "E:\\DCIM\\100GOPRO" "D:\\temp\\parked" --copy-only

    # Parallel workers (helps SSD→SSD; on SD cards try 1–2)
    python fast_move.py "E:\\DCIM\\100GOPRO" "D:\\temp\\parked" --workers 2
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BUFFER_SIZE = 16 * 1024 * 1024  # 16 MB — same as sd_offloader/offloader/transfer.py
PROGRESS_EVERY = 1 * 1024 * 1024

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_print_lock = threading.Lock()


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _same_volume(a: Path, b: Path) -> bool:
    try:
        return os.path.splitdrive(a.resolve())[0].upper() == os.path.splitdrive(b.resolve())[0].upper()
    except OSError:
        return False


def _buffered_copy(src: Path, dest: Path, *, on_progress=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    if tmp.exists():
        tmp.unlink(missing_ok=True)

    written = 0
    last_reported = 0
    read_size = (1 * 1024 * 1024) if on_progress else BUFFER_SIZE
    with src.open("rb") as reader, tmp.open("wb") as writer:
        if on_progress:
            on_progress(0)
        while True:
            chunk = reader.read(read_size)
            if not chunk:
                break
            writer.write(chunk)
            written += len(chunk)
            if on_progress and (
                written - last_reported >= PROGRESS_EVERY or len(chunk) < read_size
            ):
                on_progress(written)
                last_reported = written
        writer.flush()
        try:
            os.fsync(writer.fileno())
        except OSError:
            pass

    if not tmp.exists() or tmp.stat().st_size != src.stat().st_size:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Copy size mismatch for {src.name}")

    if dest.exists():
        dest.unlink()
    tmp.replace(dest)


def _collect_files(src: Path) -> list[Path]:
    files: list[Path] = []
    for root, _dirs, names in os.walk(src):
        for name in names:
            files.append(Path(root) / name)
    return sorted(files)


def _matches_any(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(name.upper(), p.upper()) for p in patterns)


def _should_keep(rel: Path, keep: list[str]) -> bool:
    """Keep named file and its .segments.json sidecar."""
    if not keep:
        return False
    name_u = rel.name.upper()
    for k in keep:
        k_name = Path(k).name
        if name_u == k_name.upper():
            return True
        # --keep GX010001.MP4 also keeps GX010001.segments.json
        video_stem = Path(k_name).stem.upper()
        if name_u == f"{video_stem}.SEGMENTS.JSON":
            return True
    return False


def _plan(
    src: Path,
    dest: Path,
    *,
    keep: list[str],
    only: list[str],
) -> list[tuple[Path, Path, int]]:
    planned: list[tuple[Path, Path, int]] = []
    for path in _collect_files(src):
        rel = path.relative_to(src)
        if _should_keep(rel, keep):
            continue
        if only and not _matches_any(rel.name, only) and not _matches_any(str(rel).replace("\\", "/"), only):
            continue
        size = path.stat().st_size
        planned.append((path, dest / rel, size))
    return planned


def _move_one(
    src_file: Path,
    dest_file: Path,
    *,
    copy_only: bool,
    same_vol: bool,
    index: int,
    total: int,
    shared: dict,
) -> tuple[str, int]:
    size = src_file.stat().st_size
    label = f"[{index}/{total}] {src_file.name}"

    if dest_file.exists() and dest_file.stat().st_size == size:
        if not copy_only:
            src_file.unlink()
        with _print_lock:
            print(f"  skip (already at dest): {label}")
        return ("skip", size)

    t0 = time.perf_counter()

    def on_progress(written: int) -> None:
        with shared["lock"]:
            # live file progress is noisy with workers; only update totals
            shared["file_done"] = written

    if same_vol and not copy_only:
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        if dest_file.exists():
            dest_file.unlink()
        shutil.move(str(src_file), str(dest_file))
        elapsed = max(1e-6, time.perf_counter() - t0)
        with _print_lock:
            print(f"  moved (rename): {label}  {_human(size)}  {_human(size / elapsed)}/s")
        with shared["lock"]:
            shared["bytes_done"] += size
        return ("moved", size)

    _buffered_copy(src_file, dest_file, on_progress=on_progress if shared.get("workers", 1) == 1 else None)
    elapsed = max(1e-6, time.perf_counter() - t0)
    rate = size / elapsed

    if not copy_only:
        if dest_file.stat().st_size != size:
            raise RuntimeError(f"Verify failed before delete: {src_file}")
        src_file.unlink()
        action = "moved"
    else:
        action = "copied"

    with _print_lock:
        print(f"  {action}: {label}  {_human(size)}  {_human(rate)}/s")
    with shared["lock"]:
        shared["bytes_done"] += size
    return (action, size)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fast move/copy a folder (16 MB buffered, like SD Offloader)."
    )
    parser.add_argument("source", type=Path, help="Source folder (e.g. SD card GOPRO folder)")
    parser.add_argument("dest", type=Path, help="Destination folder (e.g. other SSD temp)")
    parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="NAME",
        help="File to leave in source (repeatable). Also keeps matching .segments.json",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="GLOB",
        help="Only move files matching glob (e.g. *.MP4). Repeatable.",
    )
    parser.add_argument(
        "--copy-only",
        action="store_true",
        help="Copy without deleting source",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel file workers (default 1 — best for SD cards; try 2–4 for SSD→SSD)",
    )
    parser.add_argument("--dry-run", action="store_true", help="List actions only")
    args = parser.parse_args()

    src = args.source.expanduser().resolve()
    dest = args.dest.expanduser().resolve()

    if not src.is_dir():
        print(f"Source is not a folder: {src}", file=sys.stderr)
        return 1
    if src == dest or dest.is_relative_to(src) or src.is_relative_to(dest):
        print("Source and dest must be distinct and not nested.", file=sys.stderr)
        return 1

    planned = _plan(src, dest, keep=args.keep, only=args.only)
    if not planned:
        print("Nothing to move (empty, or everything matched --keep / --only).")
        return 0

    total_bytes = sum(s for _, _, s in planned)
    same_vol = _same_volume(src, dest)
    mode = "copy" if args.copy_only else ("rename" if same_vol else "copy+delete")

    print(f"Source:  {src}")
    print(f"Dest:    {dest}")
    print(f"Mode:    {mode}  |  workers={args.workers}  |  files={len(planned)}  |  {_human(total_bytes)}")
    if args.keep:
        print(f"Keeping: {', '.join(args.keep)} (+ matching .segments.json)")
    print()

    if args.dry_run:
        for src_file, dest_file, size in planned:
            rel = src_file.relative_to(src)
            print(f"  would {mode}: {rel}  ({_human(size)})")
        print(f"\nDry-run only — {_human(total_bytes)} across {len(planned)} files.")
        return 0

    dest.mkdir(parents=True, exist_ok=True)
    shared = {
        "lock": threading.Lock(),
        "bytes_done": 0,
        "file_done": 0,
        "workers": max(1, args.workers),
    }
    t_start = time.perf_counter()
    errors: list[str] = []

    def run(item: tuple[int, Path, Path, int]):
        index, src_file, dest_file, _size = item
        try:
            return _move_one(
                src_file,
                dest_file,
                copy_only=args.copy_only,
                same_vol=same_vol,
                index=index,
                total=len(planned),
                shared=shared,
            )
        except Exception as exc:  # noqa: BLE001 — report and continue
            msg = f"{src_file.name}: {exc}"
            with _print_lock:
                print(f"  ERROR: {msg}", file=sys.stderr)
            errors.append(msg)
            return ("error", 0)

    items = [(i + 1, s, d, sz) for i, (s, d, sz) in enumerate(planned)]
    workers = max(1, args.workers)

    if workers == 1:
        for item in items:
            run(item)
            done = shared["bytes_done"]
            elapsed = max(1e-6, time.perf_counter() - t_start)
            remaining = max(0, total_bytes - done)
            eta = remaining / (done / elapsed) if done else 0
            print(
                f"  progress: {_human(done)} / {_human(total_bytes)}  "
                f"({100 * done / total_bytes:.1f}%)  "
                f"avg {_human(done / elapsed)}/s  ETA {eta:.0f}s"
            )
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, item) for item in items]
            for _ in as_completed(futures):
                done = shared["bytes_done"]
                elapsed = max(1e-6, time.perf_counter() - t_start)
                print(
                    f"  progress: {_human(done)} / {_human(total_bytes)}  "
                    f"({100 * done / max(1, total_bytes):.1f}%)  "
                    f"avg {_human(done / elapsed)}/s"
                )

    elapsed = max(1e-6, time.perf_counter() - t_start)
    print()
    print(
        f"Done in {elapsed:.1f}s — {_human(shared['bytes_done'])} "
        f"at {_human(shared['bytes_done'] / elapsed)}/s avg"
    )
    if errors:
        print(f"{len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

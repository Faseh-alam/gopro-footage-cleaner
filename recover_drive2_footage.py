#!/usr/bin/env python3
"""Audit and recover Drive 2 footage after an overnight trim run.

Run:
  python3 recover_drive2_footage.py audit
  python3 recover_drive2_footage.py list-safe-trash
  python3 recover_drive2_footage.py list-restore

Trash for this drive lives at:
  /Volumes/Drive 2/.Trashes/<uid>/
Finder "Put Back" often fails on external drives when the disk is full.
"""

from __future__ import annotations

import csv
import shutil
import subprocess
import sys
from pathlib import Path

DRIVE = "Drive 2"
BASE = Path(f"/Volumes/{DRIVE}/archive/YT")
FALLBACK = Path.home() / "Movies/GoPro Cleaned Clips"
SHEETS = [
    ("23-04-26", Path("drive2/footage_sheet_Drive_2_23-04-26_filled.csv")),
    ("24-04-26", Path("footage_sheet_Drive_1_24-04-26_filled.csv")),
]


def trash_names() -> set[str]:
    script = 'tell application "Finder" to name of every item of trash'
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        return set()
    return {name.strip() for name in out.stdout.split(", ") if name.strip()}


def disk_free_mb() -> float:
    usage = shutil.disk_usage(BASE)
    return usage.free / (1024 * 1024)


def fallback_clips(stem: str) -> list[Path]:
    if not FALLBACK.exists():
        return []
    return sorted(
        clip
        for clip in FALLBACK.glob(f"{stem}-*.MP4")
        if not clip.name.startswith(".")
    )


def audit_rows(skip_trash: bool = False) -> list[dict]:
    trash = set() if skip_trash else trash_names()
    rows: list[dict] = []
    for date, sheet in SHEETS:
        if not sheet.exists():
            continue
        date_base = BASE / date
        with sheet.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                timestamps = (row.get("timestamps") or "").strip()
                if not timestamps:
                    continue
                camera = row["camera"]
                footage = row["footage"].strip()
                if not footage.upper().endswith(".MP4"):
                    footage += ".MP4"
                original = date_base / camera / footage
                stem = Path(footage).stem
                clips = sorted(
                    clip
                    for clip in (date_base / camera).glob(f"{stem}-*.MP4")
                    if not clip.name.startswith("._")
                )
                fallback = fallback_clips(stem)
                in_trash = footage in trash
                if clips and not original.exists():
                    status = "ok_clips_only"
                elif fallback and not clips:
                    status = "clips_in_movies"
                elif clips and original.exists():
                    status = "ok_both"
                elif original.exists() and not clips and not fallback:
                    status = "pending"
                elif in_trash and not clips and not fallback:
                    status = "restore_from_trash"
                else:
                    status = "missing"
                rows.append(
                    {
                        "date": date,
                        "camera": camera,
                        "footage": footage,
                        "status": status,
                        "clips": clips,
                        "fallback": fallback,
                        "original": original,
                        "in_trash": in_trash,
                    }
                )
    return rows


def print_audit() -> None:
    free = disk_free_mb()
    print(f"Drive: {DRIVE}")
    print(f"Free space: {free:.1f} MB")
    if free < 1024:
        print("WARNING: drive is nearly full. Free space before restoring files.")
    rows = audit_rows()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print("\nStatus summary:")
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")
    print("\nClips saved to ~/Movies/GoPro Cleaned Clips (drive was full):")
    for row in rows:
        if row["status"] == "clips_in_movies":
            names = ", ".join(clip.name for clip in row["fallback"])
            print(f"  {row['date']} {row['camera']}/{row['footage']} -> {names}")
    print("\nRestore from trash (no clips anywhere yet):")
    for row in rows:
        if row["status"] == "restore_from_trash":
            print(f"  {row['date']} {row['camera']}/{row['footage']}")
    print("\nSafe to delete from Trash (clips already on drive):")
    for row in rows:
        if row["status"] == "ok_clips_only" and row["in_trash"]:
            clip_count = len(row["clips"])
            print(f"  {row['date']} {row['camera']}/{row['footage']} ({clip_count} clips)")


def safe_trash_deletions(rows: list[dict]) -> list[str]:
    """Originals in Trash where trimmed clips already exist (on drive or in Movies)."""
    names: list[str] = []
    for row in rows:
        if not row["in_trash"]:
            continue
        if row["status"] in {"ok_clips_only", "clips_in_movies"}:
            names.append(row["footage"])
    return sorted(set(names))


def delete_from_trash(filenames: list[str]) -> tuple[int, list[str]]:
    deleted = 0
    errors: list[str] = []
    for name in filenames:
        script = f'''
        tell application "Finder"
            try
                delete (first item of trash whose name is "{name}")
                return "ok"
            on error errMsg number errNum
                return "fail:" & errNum & ":" & errMsg
            end try
        end tell
        '''
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
        result = (out.stdout or out.stderr).strip()
        if result == "ok":
            deleted += 1
        else:
            errors.append(f"{name}: {result}")
    return deleted, errors


def _find_clip_on_drive(clip_name: str) -> Path | None:
    matches = [
        path
        for path in BASE.rglob(clip_name)
        if path.is_file() and not path.name.startswith("._")
    ]
    if not matches:
        return None
    return matches[0]


def move_fallback_clips(rows: list[dict]) -> tuple[int, list[str]]:
    moved = 0
    errors: list[str] = []
    pending = [row for row in rows if row["status"] == "clips_in_movies"]
    total = sum(len(row["fallback"]) for row in pending)
    print(f"Moving {total} clip files from Movies to Drive 2 (this can take a while)...", flush=True)
    done = 0
    for row in pending:
        dest_dir = row["original"].parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        for clip in row["fallback"]:
            dest = dest_dir / clip.name
            if dest.exists() and dest.stat().st_size > 0:
                done += 1
                continue
            if not clip.exists():
                # Already moved for another camera folder with the same stem.
                donor = _find_clip_on_drive(clip.name)
                if donor is None:
                    errors.append(f"{clip.name}: source missing and not found on Drive 2")
                    print(f"  skip missing source: {clip.name}", flush=True)
                    done += 1
                    continue
                size_gb = donor.stat().st_size / (1024**3)
                print(
                    f"  [{done + 1}/{total}] {clip.name} ({size_gb:.1f} GB) copy from {donor.parent}",
                    flush=True,
                )
                try:
                    shutil.copy2(donor, dest)
                    moved += 1
                    done += 1
                    print("       done", flush=True)
                except OSError as exc:
                    errors.append(f"{clip.name} -> {dest_dir}: {exc}")
                    print(f"       FAILED: {exc}", flush=True)
                    done += 1
                continue
            size_gb = clip.stat().st_size / (1024**3)
            print(f"  [{done + 1}/{total}] {clip.name} ({size_gb:.1f} GB) -> {dest_dir}", flush=True)
            try:
                shutil.move(str(clip), str(dest))
                moved += 1
                done += 1
                print(f"       done", flush=True)
            except OSError as exc:
                errors.append(f"{clip.name} -> {dest_dir}: {exc}")
                print(f"       FAILED: {exc}", flush=True)
                done += 1
    return moved, errors


def run_recovery() -> None:
    print("Step 1: Freeing space — removing trashed originals that already have clips...")
    rows = audit_rows()
    to_delete = safe_trash_deletions(rows)
    print(f"  {len(to_delete)} Trash items safe to remove")
    deleted, del_errors = delete_from_trash(to_delete)
    print(f"  Removed {deleted} items from Trash")
    for err in del_errors[:10]:
        print(f"  ! {err}")
    if len(del_errors) > 10:
        print(f"  ! ... and {len(del_errors) - 10} more errors")

    free = disk_free_mb()
    print(f"\nDrive free space now: {free:.0f} MB")

    print("\nStep 2: Moving clips from ~/Movies/GoPro Cleaned Clips to Drive 2...")
    rows = audit_rows()
    moved, move_errors = move_fallback_clips(rows)
    print(f"  Moved {moved} clip files to Drive 2")
    for err in move_errors[:10]:
        print(f"  ! {err}")
    if len(move_errors) > 10:
        print(f"  ! ... and {len(move_errors) - 10} more errors")

    free = disk_free_mb()
    print(f"\nDrive free space now: {free:.0f} MB")
    print("\nStep 3: Final status")
    print_audit()


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "audit"
    if command == "audit":
        print_audit()
        return
    if command == "recover":
        run_recovery()
        return
    if command == "write-trash-script":
        script = Path("free_drive2_trash.sh")
        if not script.exists():
            raise SystemExit("free_drive2_trash.sh not found")
        names = safe_trash_deletions(audit_rows())
        print(f"Run in Terminal.app (with Full Disk Access):\n")
        print(f"  cd \"{Path.cwd()}\"")
        print(f"  bash free_drive2_trash.sh")
        print(f"\nWill delete {len(names)} safe Trash items.")
        return
    if command == "move-clips":
        rows = audit_rows(skip_trash=True)
        moved, move_errors = move_fallback_clips(rows)
        print(f"\nMoved {moved} clip files to Drive 2")
        for err in move_errors:
            print(f"  ! {err}")
        return
    if command == "list-safe-trash":
        for row in audit_rows():
            if row["status"] in {"ok_clips_only", "clips_in_movies"} and row["in_trash"]:
                print(row["footage"])
        return
    if command == "list-restore":
        for row in audit_rows():
            if row["status"] == "restore_from_trash":
                print(f"{row['date']},{row['camera']},{row['footage']}")
        return
    raise SystemExit(f"Unknown command: {command}")


if __name__ == "__main__":
    main()

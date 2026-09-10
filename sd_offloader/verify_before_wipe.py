#!/usr/bin/env python3
"""Authoritative pre-wipe verification: is EVERY local file for this batch,
across BOTH configured SSDs, actually and correctly present on S3?

Why this exists: the app's own "Verify sizes" check is scoped to a single AWS
job's stored `sources` snapshot, captured once when that job was created. If a
batch's local footprint later grows onto a second SSD (a very normal thing —
see engine.py's dual-SSD spillover), an old job's "verified: true" can be
blind to that second SSD entirely. This script re-derives the batch's CURRENT
local footprint fresh every run, checks it three independent ways, and only
says "safe to wipe" if all three agree.

Runs on Mac or Windows (pure stdlib + the `aws` CLI, already a hard
requirement for this whole pipeline) — same tool, same checks, in the demo
here and on the real production box later.

Usage:
    python3 verify_before_wipe.py <batch-name> [s3-uri]

If s3-uri is omitted, reads it from config.json. ssd1/ssd2 always come from
config.json (the same file the running app uses) unless overridden with the
SSD1 / SSD2 environment variables.

Exit code 0 = safe to wipe. Exit code 1 = NOT safe — do not wipe, details printed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from offloader.aws_upload import batch_s3_prefix, normalize_s3_uri  # noqa: E402
from offloader.config import load_config  # noqa: E402


def die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def dir_stats(root: Path) -> tuple[int, int]:
    """(file_count, total_bytes) for every file under root."""
    count = 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            count += 1
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return count, total


def run_dry_run_sync(local_dir: Path, s3_prefix: str) -> list[str]:
    """Return the list of file paths `aws s3 sync --dryrun` says it would upload."""
    local_arg = str(local_dir)
    if not local_arg.endswith(os.sep):
        local_arg += os.sep
    result = subprocess.run(
        ["aws", "s3", "sync", local_arg, s3_prefix, "--dryrun"],
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    pending = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("(dryrun) upload:"):
            pending.append(line)
    return pending


def s3_listing_summary(s3_prefix: str) -> tuple[int, int]:
    """(object_count, total_bytes) currently under s3_prefix, via `aws s3 ls --summarize`."""
    result = subprocess.run(
        ["aws", "s3", "ls", s3_prefix, "--recursive", "--summarize"],
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    objects = 0
    size = 0
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Total Objects:"):
            objects = int(line.split(":", 1)[1].strip() or 0)
        elif line.startswith("Total Size:"):
            size = int(line.split(":", 1)[1].strip() or 0)
    return objects, size


def main() -> None:
    if len(sys.argv) < 2:
        die("Usage: python3 verify_before_wipe.py <batch-name> [s3-uri]")

    batch = sys.argv[1].strip()
    cfg = load_config()
    s3_uri = sys.argv[2].strip() if len(sys.argv) > 2 else str(cfg.get("s3_uri") or "")
    ssd1 = os.environ.get("SSD1") or str(cfg.get("ssd1") or "")
    ssd2 = os.environ.get("SSD2") or str(cfg.get("ssd2") or "")

    if not s3_uri:
        die("No S3 URI given and none found in config.json")
    if not ssd1 and not ssd2:
        die("No SSD1/SSD2 found in config.json and none given via SSD1/SSD2 env vars")

    try:
        s3_prefix = batch_s3_prefix(s3_uri, batch)
    except ValueError as exc:
        die(str(exc))

    print("=" * 72)
    print(f" Pre-wipe verification — batch: {batch}")
    print(f" S3 target: {s3_prefix}")
    print("=" * 72)

    overall_ok = True
    total_local_files = 0
    total_local_bytes = 0

    for label, ssd in (("SSD1", ssd1), ("SSD2", ssd2)):
        if not ssd:
            print(f"\n-- {label}: not configured, skipping")
            continue

        batch_dir = Path(ssd) / "Batches" / batch
        print(f"\n-- {label}: {batch_dir}")
        if not batch_dir.is_dir():
            print("   No Batches/<batch> folder here — nothing to verify on this drive")
            continue

        file_count, byte_count = dir_stats(batch_dir)
        total_local_files += file_count
        total_local_bytes += byte_count
        print(f"   Local files: {file_count}   Local bytes: {byte_count}")

        print("   Running authoritative dry-run sync check (aws s3 sync --dryrun)...")
        pending = run_dry_run_sync(batch_dir, s3_prefix)
        if pending:
            overall_ok = False
            print(f"   NOT SAFE — {len(pending)} file(s) on {label} missing/different on S3:")
            for line in pending:
                print(f"      {line}")
        else:
            print(f"   OK — every file on {label} already matches S3.")

    print("\n-- Cross-check: object count")
    s3_objects, s3_bytes = s3_listing_summary(s3_prefix)
    print(f"   Local files (all SSDs): {total_local_files}   S3 objects: {s3_objects}")
    if s3_objects != total_local_files:
        overall_ok = False
        print("   MISMATCH — counts differ, investigate before wiping.")
    else:
        print("   OK — counts match.")

    print("\n-- Cross-check: byte totals")
    print(f"   Local total bytes: {total_local_bytes}   S3 total bytes: {s3_bytes}")
    delta = abs(total_local_bytes - s3_bytes)
    tolerance = 1024 * 1024  # 1 MiB, same tolerance the app itself uses
    if delta > tolerance:
        overall_ok = False
        print(f"   MISMATCH — Δ {delta} bytes exceeds {tolerance}-byte tolerance.")
    else:
        print(f"   OK — Δ {delta} bytes, within tolerance.")

    print("\n" + "=" * 72)
    if overall_ok:
        print(" RESULT: SAFE TO WIPE — every local file across both SSDs is confirmed on S3.")
        sys.exit(0)
    else:
        print(" RESULT: DO NOT WIPE — see failures above.")
        sys.exit(1)


if __name__ == "__main__":
    main()

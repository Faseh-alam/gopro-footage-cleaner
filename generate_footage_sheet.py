#!/usr/bin/env python3
"""Generate pre-filled trim sheets from a drive's archive folders."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from gopro_cleaner.core.inventory import (
    list_archive_sections,
    safe_name,
    scan_all_archive,
    scan_archive_section,
    write_sheet,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate footage trim sheets from a drive")
    parser.add_argument("drive", help='Drive name exactly as in Finder, e.g. "Drive 1"')
    parser.add_argument(
        "-o",
        "--output",
        help="Output CSV path",
    )
    parser.add_argument(
        "--archive",
        help='Archive folder under archive/, e.g. "YT", "Multinet", "Tasks"',
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan every folder under archive/ (Multinet, Tasks, YT, etc.)",
    )
    parser.add_argument(
        "--date",
        help="Only include one date folder, e.g. 26-04-26",
    )
    parser.add_argument(
        "--output-dir",
        help="Write one full sheet per archive section into this directory",
    )
    args = parser.parse_args()

    if args.output_dir:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        all_rows = []
        for archive in list_archive_sections(args.drive):
            rows = scan_archive_section(args.drive, archive)
            if not rows:
                continue
            section_dir = out_dir / safe_name(archive)
            write_sheet(rows, section_dir / "footage_sheet_full.csv")
            dates = sorted({row["date"] for row in rows})
            for date in dates:
                date_rows = [row for row in rows if row["date"] == date]
                if date_rows:
                    write_sheet(date_rows, section_dir / f"footage_sheet_{date}.csv")
            all_rows.extend(rows)
            print(f"{archive}: {len(rows)} videos")
        if all_rows:
            write_sheet(all_rows, out_dir / "footage_sheet_all_archive.csv")
            print(f"All archive: {len(all_rows)} videos -> {out_dir / 'footage_sheet_all_archive.csv'}")
        return

    if args.all:
        rows = scan_all_archive(args.drive)
        archive_label = "all_archive"
    elif args.archive:
        rows = scan_archive_section(args.drive, args.archive)
        archive_label = safe_name(args.archive)
    else:
        rows = scan_archive_section(args.drive, "YT")
        archive_label = "YT"

    if args.date:
        rows = [row for row in rows if row["date"] == args.date]

    if not rows:
        raise SystemExit("No raw footage files found.")

    safe_drive = safe_name(args.drive)
    if args.output:
        output = Path(args.output)
    elif args.date:
        output = Path(f"footage_sheet_{safe_drive}_{archive_label}_{args.date}.csv")
    else:
        output = Path(f"footage_sheet_{safe_drive}_{archive_label}.csv")

    write_sheet(rows, output)
    groups = len({(row["archive"], row["date"], row["camera"]) for row in rows})
    print(f"Wrote {len(rows)} videos ({groups} groups) to {output}")


if __name__ == "__main__":
    main()

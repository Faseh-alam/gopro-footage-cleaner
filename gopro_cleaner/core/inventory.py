"""Scan drives and build pre-filled footage inventory sheets."""

from __future__ import annotations

import csv
import re
from pathlib import Path

CLIP_NAME_RE = re.compile(r"-\d+\.(mp4|mov|360|mkv)$", re.IGNORECASE)
VIDEO_EXTENSIONS = {".mp4", ".mov", ".360", ".mkv"}
SHEET_FIELDS = ["archive", "date", "camera", "task", "footage", "timestamps"]


def is_raw_footage(path: Path) -> bool:
    if path.name.startswith("._"):
        return False
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    return not CLIP_NAME_RE.search(path.name)


def archive_root(drive: str) -> Path:
    return Path("/Volumes") / drive.strip() / "archive"


def list_archive_sections(drive: str) -> list[str]:
    root = archive_root(drive)
    if not root.exists():
        raise FileNotFoundError(f"Folder not found: {root}")
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    )


def _scan_section_path(section_path: Path, archive_name: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not section_path.exists():
        return rows

    for date_dir in sorted(section_path.iterdir(), key=lambda p: p.name.lower()):
        if not date_dir.is_dir() or date_dir.name.startswith("."):
            continue

        date = date_dir.name
        for camera_dir in sorted(date_dir.iterdir(), key=lambda p: p.name.lower()):
            if not camera_dir.is_dir() or camera_dir.name.startswith("."):
                continue

            camera = camera_dir.name
            for item in sorted(camera_dir.iterdir(), key=lambda p: p.name.lower()):
                if item.name.startswith("._"):
                    continue
                if item.is_file() and is_raw_footage(item):
                    rows.append(
                        {
                            "archive": archive_name,
                            "date": date,
                            "camera": camera,
                            "task": "",
                            "footage": item.name,
                            "timestamps": "",
                        }
                    )
                elif item.is_dir():
                    task = item.name
                    for video in sorted(item.iterdir(), key=lambda p: p.name.lower()):
                        if video.is_file() and is_raw_footage(video):
                            rows.append(
                                {
                                    "archive": archive_name,
                                    "date": date,
                                    "camera": camera,
                                    "task": task,
                                    "footage": video.name,
                                    "timestamps": "",
                                }
                            )
    return rows


def scan_archive_section(drive: str, archive: str) -> list[dict[str, str]]:
    section_path = archive_root(drive) / archive
    if not section_path.exists():
        raise FileNotFoundError(f"Folder not found: {section_path}")
    return _scan_section_path(section_path, archive)


def scan_drive(drive: str, archive: str = "YT") -> list[dict[str, str]]:
    """Scan one archive section (default YT for backward compatibility)."""
    return scan_archive_section(drive, archive)


def scan_all_archive(drive: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for archive in list_archive_sections(drive):
        rows.extend(scan_archive_section(drive, archive))
    return rows


def write_sheet(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name.strip())

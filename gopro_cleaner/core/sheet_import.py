"""Parse helper trim sheets (CSV / JSON) and resolve video paths."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .timestamps import parse_timestamp

VIDEO_EXTENSIONS = {".mp4", ".mov", ".360", ".mkv"}
TRUTHY = {"yes", "y", "true", "1", "delete"}
FALSY = {"no", "n", "false", "0", "keep", ""}
RANGE_SPLIT_RE = re.compile(r"\s*(?:-|–|—|->|to)\s*", re.IGNORECASE)
TIMESTAMP_LIST_RE = re.compile(r"[,;|]+")


@dataclass
class ClipRow:
    start: str
    end: str
    start_seconds: float
    end_seconds: float


@dataclass
class VideoImport:
    video_path: Path
    footage: str
    video: str
    clips: list[ClipRow] = field(default_factory=list)
    delete_original: bool = True
    row_numbers: list[int] = field(default_factory=list)
    drive: str = ""
    date: str = ""
    camera: str = ""
    task: str = ""
    archive: str = "YT"


@dataclass
class ImportPreview:
    videos: list[VideoImport]
    errors: list[str]
    warnings: list[str]
    drive: str = ""


def _parse_bool(value: str | bool | None, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    raise ValueError(f"Invalid yes/no value: {value}")


def _normalize_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


CSV_ALIASES = {
    "footage": "footage",
    "footage_name": "footage",
    "video_name": "footage",
    "file_name": "footage",
    "name": "footage",
    "timestamps": "timestamps",
    "timestamp": "timestamps",
    "times": "timestamps",
    "clips": "timestamps",
    "clip_times": "timestamps",
    "start": "start",
    "start_time": "start",
    "end": "end",
    "end_time": "end",
    "delete_original": "delete_original",
    "notes": "notes",
    "date": "date",
    "date_folder": "date",
    "folder_date": "date",
    "archive": "archive",
    "client": "archive",
    "section": "archive",
    "folder": "archive",
    "camera": "camera",
    "camera_serial": "camera",
    "serial": "camera",
    "task": "task",
    "task_folder": "task",
    "video": "video",
    "video_path": "video_path",
    "path": "video_path",
    "drive": "drive",
}


def _map_csv_row(row: dict[str, str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        normalized = _normalize_header(key)
        canonical = CSV_ALIASES.get(normalized)
        if canonical:
            mapped[canonical] = (value or "").strip()
    return mapped


def _is_comment_row(row: dict[str, str]) -> bool:
    first = next((value for value in row.values() if value and value.strip()), "")
    return first.startswith("#")


def _clip_from_values(start_raw: str, end_raw: str, line_ref: str) -> ClipRow:
    start_raw = start_raw.strip()
    end_raw = end_raw.strip()
    if not start_raw or not end_raw:
        raise ValueError(f"{line_ref}: start and end are required")

    start_seconds = parse_timestamp(start_raw)
    end_seconds = parse_timestamp(end_raw)
    if end_seconds <= start_seconds:
        raise ValueError(f"{line_ref}: end must be after start")

    return ClipRow(
        start=start_raw,
        end=end_raw,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )


def _normalize_timestamp_text(text: str) -> str:
    """Fix common helper typos like '6.46 . 35.14' or '10.04. 17.39'."""
    text = text.strip()
    text = re.sub(r"\s*\.\s+(?=\d)", " - ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    text = re.sub(r"\s*-\s*", " - ", text)
    return text


def _clips_from_timestamps_cell(text: str, line_ref: str) -> list[ClipRow]:
    text = _normalize_timestamp_text(text)
    if not text:
        return []

    clips: list[ClipRow] = []
    for chunk in TIMESTAMP_LIST_RE.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = RANGE_SPLIT_RE.split(chunk, maxsplit=1)
        if len(parts) != 2:
            raise ValueError(
                f"{line_ref}: use ranges like '00:00 - 7:45, 10:00 - 12:00'"
            )
        clips.append(_clip_from_values(parts[0], parts[1], line_ref))

    return clips


def _archive_base(drive: str, archive: str = "YT") -> Path:
    drive = drive.strip()
    archive = (archive or "YT").strip()
    if not drive:
        raise ValueError("Choose which drive this sheet is for")
    return Path("/Volumes") / drive / "archive" / archive


def find_footage(drive: str, footage: str, archive: str = "") -> Path:
    footage = footage.strip().replace("\\", "/").strip("/")
    if not footage:
        raise ValueError("footage name is required")

    if footage.startswith("/Volumes/"):
        path = Path(footage).expanduser().resolve()
        if path.exists():
            return path
        raise FileNotFoundError(f"Video not found: {footage}")

    search_roots: list[Path] = []
    if archive:
        search_roots.append(_archive_base(drive, archive))
    else:
        archive_root = Path("/Volumes") / drive.strip() / "archive"
        if archive_root.exists():
            search_roots.extend(
                child for child in sorted(archive_root.iterdir())
                if child.is_dir() and not child.name.startswith(".")
            )

    if not search_roots:
        raise FileNotFoundError(f"No archive folders found on {drive}")

    if "/" in footage:
        for root in search_roots:
            candidate = (root / footage).resolve()
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"Video not found: {footage}")

    matches: list[Path] = []
    for root in search_roots:
        for path in root.rglob(footage):
            if (
                path.is_file()
                and not path.name.startswith("._")
                and path.suffix.lower() in VIDEO_EXTENSIONS
            ):
                matches.append(path.resolve())

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        examples = "\n".join(f"  - {match}" for match in matches[:5])
        extra = f"\n  ... and {len(matches) - 5} more" if len(matches) > 5 else ""
        raise ValueError(
            f"'{footage}' found in {len(matches)} places. "
            f"Include archive/date/camera in the sheet row.\n{examples}{extra}"
        )
    raise FileNotFoundError(
        f"'{footage}' not found on {drive}. "
        "Check the file name or fill in archive, date, and camera columns."
    )


def resolve_video_path(
    drive: str,
    date: str,
    camera: str,
    video: str,
    task: str = "",
    archive: str = "YT",
) -> Path:
    date = date.strip().strip("/")
    camera = camera.strip().strip("/")
    task = task.strip().strip("/")
    video = video.strip()
    archive = (archive or "YT").strip()
    base = _archive_base(drive, archive)
    relative = Path(date) / camera
    if task:
        relative = relative / task
    return (base / relative / video).resolve()


def _sheet_format(mapped_rows: list[dict[str, str]]) -> str:
    keys = set()
    for row in mapped_rows:
        keys.update(row.keys())
    if {"date", "camera", "footage"} <= keys:
        return "inventory"
    if "footage" in keys and "timestamps" in keys:
        return "simple_pair"
    if "footage" in keys and "start" in keys and "end" in keys:
        return "simple_triple"
    if {"drive", "date", "camera", "video"} & keys:
        return "legacy"
    if "video_path" in keys:
        return "legacy"
    if "footage" in keys:
        return "simple_triple"
    raise ValueError(
        "Sheet must have columns: date + camera + footage + timestamps, "
        "or footage + timestamps, or footage + start + end"
    )


def _clips_for_row(row: dict[str, str], line_ref: str, sheet_format: str) -> list[ClipRow]:
    if sheet_format in {"simple_pair", "inventory"}:
        clips = _clips_from_timestamps_cell(row.get("timestamps", ""), line_ref)
        if not clips:
            return []
        return clips
    return [_clip_from_values(row.get("start", ""), row.get("end", ""), line_ref)]


def _footage_for_row(row: dict[str, str], sheet_format: str) -> str:
    if sheet_format == "inventory":
        footage = row.get("footage", "").strip()
        if footage:
            return footage
        raise ValueError("footage name is required")
    if sheet_format.startswith("simple"):
        footage = row.get("footage", "").strip()
        if footage:
            return footage
        raise ValueError("footage name is required")
    if row.get("video_path"):
        return row["video_path"]
    video = row.get("video", "").strip()
    if video:
        return video
    raise ValueError("footage / video name is required")


def _resolve_row_path(
    row: dict[str, str],
    sheet_format: str,
    drive: str,
) -> tuple[Path, str]:
    if sheet_format == "inventory":
        row_drive = row.get("drive") or drive
        path = resolve_video_path(
            row_drive,
            row.get("date", ""),
            row.get("camera", ""),
            row.get("footage", ""),
            row.get("task", ""),
            row.get("archive", "YT"),
        )
        return path, row.get("footage", path.name)

    footage = _footage_for_row(row, sheet_format)

    if sheet_format == "legacy":
        if row.get("video_path"):
            path = Path(row["video_path"]).expanduser().resolve()
            return path, path.name
        row_drive = row.get("drive") or drive
        path = resolve_video_path(
            row_drive,
            row.get("date", ""),
            row.get("camera", ""),
            row.get("video", ""),
            row.get("task", ""),
            row.get("archive", "YT"),
        )
        return path, row.get("video", path.name)

    path = find_footage(drive, footage, row.get("archive", "") if sheet_format == "simple_pair" else "")
    return path, footage


def parse_csv_sheet(text: str, drive: str = "", delete_original: bool = True) -> ImportPreview:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV sheet is missing a header row")

    raw_rows: list[tuple[int, dict[str, str]]] = []
    for line_number, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            continue
        if _is_comment_row(row):
            continue
        raw_rows.append((line_number, _map_csv_row(row)))

    if not raw_rows:
        raise ValueError("Sheet has no data rows")

    sheet_format = _sheet_format([row for _, row in raw_rows])
    return _build_preview_from_rows(
        raw_rows,
        "CSV",
        sheet_format,
        drive,
        delete_original,
    )


def parse_json_sheet(text: str, drive: str = "", delete_original: bool = True) -> ImportPreview:
    payload = json.loads(text)
    defaults = payload.get("defaults", {}) if isinstance(payload, dict) else {}
    default_delete = _parse_bool(defaults.get("delete_original"), default=delete_original)
    default_drive = str(defaults.get("drive", drive)).strip()

    videos_payload = []
    if isinstance(payload, dict) and "videos" in payload:
        videos_payload = payload["videos"]
    elif isinstance(payload, list):
        videos_payload = payload
    else:
        raise ValueError("JSON must be a list of videos or an object with a 'videos' array")

    raw_rows: list[tuple[int, dict[str, str]]] = []
    for video_index, item in enumerate(videos_payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Video entry {video_index} must be an object")

        row = {
            "footage": str(item.get("footage", item.get("video", ""))),
            "timestamps": str(item.get("timestamps", "")),
            "start": str(item.get("start", "")),
            "end": str(item.get("end", "")),
            "drive": str(item.get("drive", default_drive)),
            "date": str(item.get("date", "")),
            "camera": str(item.get("camera", "")),
            "task": str(item.get("task", "")),
            "video": str(item.get("video", "")),
            "video_path": str(item.get("video_path", item.get("path", ""))),
            "delete_original": "yes"
            if _parse_bool(item.get("delete_original", default_delete), default_delete)
            else "no",
        }

        if item.get("timestamps"):
            row["timestamps"] = str(item["timestamps"])
            raw_rows.append((video_index, row))
        elif item.get("clips"):
            for clip_index, clip in enumerate(item["clips"], start=1):
                clip_row = dict(row)
                clip_row["start"] = str(clip.get("start", ""))
                clip_row["end"] = str(clip.get("end", ""))
                clip_row["timestamps"] = ""
                raw_rows.append((video_index * 1000 + clip_index, clip_row))
        elif item.get("start") and item.get("end"):
            row["start"] = str(item["start"])
            row["end"] = str(item["end"])
            raw_rows.append((video_index, row))
        else:
            raise ValueError(f"Video entry {video_index} needs timestamps or start/end")

    sheet_format = _sheet_format([row for _, row in raw_rows])
    return _build_preview_from_rows(
        raw_rows,
        "JSON",
        sheet_format,
        default_drive,
        default_delete,
    )


def _build_preview_from_rows(
    raw_rows: list[tuple[int, dict[str, str]]],
    source: str,
    sheet_format: str,
    drive: str,
    default_delete_original: bool,
) -> ImportPreview:
    grouped: dict[str, VideoImport] = {}
    errors: list[str] = []
    warnings: list[str] = []

    if sheet_format.startswith("simple") and not drive:
        errors.append("Choose which drive this sheet is for before importing")
    if sheet_format == "inventory" and not drive:
        errors.append("Choose which drive this sheet is for before importing")

    for line_number, row in raw_rows:
        line_ref = f"{source} line {line_number}"
        try:
            if errors and (sheet_format.startswith("simple") or sheet_format == "inventory") and not drive:
                continue

            clips = _clips_for_row(row, line_ref, sheet_format)
            if not clips:
                continue

            video_path, footage_label = _resolve_row_path(row, sheet_format, drive)
            delete_original = _parse_bool(
                row.get("delete_original"),
                default=default_delete_original,
            )
            key = str(video_path)

            if key not in grouped:
                grouped[key] = VideoImport(
                    video_path=video_path,
                    footage=footage_label,
                    video=video_path.name,
                    delete_original=delete_original,
                    row_numbers=[line_number],
                    drive=drive or row.get("drive", ""),
                    date=row.get("date", ""),
                    camera=row.get("camera", ""),
                    task=row.get("task", ""),
                    archive=row.get("archive", "YT"),
                )
            else:
                entry = grouped[key]
                entry.row_numbers.append(line_number)

            grouped[key].clips.extend(clips)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{line_ref}: {exc}")

    videos = list(grouped.values())
    if not videos and not errors:
        warnings.append("No rows have timestamps filled in yet")

    for entry in videos:
        if not entry.video_path.exists():
            errors.append(f"Video not found: {entry.footage}")
        elif entry.video_path.suffix.lower() not in VIDEO_EXTENSIONS:
            warnings.append(f"Unusual video extension: {entry.video_path.name}")

    return ImportPreview(
        videos=videos,
        errors=errors,
        warnings=warnings,
        drive=drive,
    )


def parse_sheet(
    text: str,
    filename: str = "",
    drive: str = "",
    delete_original: bool = True,
) -> ImportPreview:
    name = filename.lower()
    stripped = text.strip()
    if name.endswith(".json") or stripped.startswith("{") or stripped.startswith("["):
        return parse_json_sheet(text, drive, delete_original)
    return parse_csv_sheet(text, drive, delete_original)


def preview_to_dict(preview: ImportPreview) -> dict:
    return {
        "drive": preview.drive,
        "video_count": len(preview.videos),
        "clip_count": sum(len(video.clips) for video in preview.videos),
        "errors": preview.errors,
        "warnings": preview.warnings,
        "ready": not preview.errors and bool(preview.videos),
        "videos": [
            {
                "footage": video.footage,
                "archive": video.archive,
                "video_path": str(video.video_path),
                "video": video.video,
                "delete_original": video.delete_original,
                "clip_count": len(video.clips),
                "clips": [{"start": clip.start, "end": clip.end} for clip in video.clips],
                "rows": video.row_numbers,
            }
            for video in preview.videos
        ],
    }


def queue_import(preview: ImportPreview, trim_queue) -> dict:
    if preview.errors:
        raise ValueError("Fix sheet errors before queueing")

    queued = []
    failures = []
    for entry in preview.videos:
        try:
            clips = [(clip.start_seconds, clip.end_seconds) for clip in entry.clips]
            batch = trim_queue.submit_batch(
                entry.video_path,
                clips,
                entry.delete_original,
            )
            queued.append(
                {
                    "footage": entry.footage,
                    "video_path": str(entry.video_path),
                    "clip_count": len(entry.clips),
                    "batch_id": batch.batch_id,
                }
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "footage": entry.footage,
                    "video_path": str(entry.video_path),
                    "error": str(exc),
                }
            )

    return {
        "queued_count": len(queued),
        "failed_count": len(failures),
        "clip_count": sum(item["clip_count"] for item in queued),
        "queued": queued,
        "failures": failures,
    }

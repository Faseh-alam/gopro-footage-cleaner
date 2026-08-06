"""Parse flexible timestamp strings into seconds."""

from __future__ import annotations

import re


_TIMESTAMP_RE = re.compile(
    r"^(?:(?P<hours>\d+)\s*(?:h|hours?|hrs?))?\s*"
    r"(?:(?P<minutes>\d+)\s*(?:m|min(?:ute)?s?))?\s*"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)\s*(?:s|sec(?:ond)?s?))?$",
    re.IGNORECASE,
)


def parse_timestamp(value: str) -> float:
    """Convert a user-provided timestamp into seconds.

    Supported formats:
    - 450
    - 7:30
    - 00:07:30
    - 21.03  (21 minutes 3 seconds — helper sheet style)
    - 7m30s
    """
    text = value.strip()
    if not text:
        raise ValueError("Timestamp cannot be empty")

    if ":" in text:
        parts = text.split(":")
        if len(parts) > 3:
            raise ValueError(f"Invalid timestamp: {value}")
        try:
            nums = [float(part) for part in parts]
        except ValueError as exc:
            raise ValueError(f"Invalid timestamp: {value}") from exc
        if len(nums) == 1:
            return nums[0]
        if len(nums) == 2:
            minutes, seconds = nums
            return minutes * 60 + seconds
        hours, minutes, seconds = nums
        return hours * 3600 + minutes * 60 + seconds

    if "." in text:
        parts = text.split(".", 1)
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            minutes = int(parts[0])
            seconds = int(parts[1])
            if seconds < 60:
                return minutes * 60 + seconds

    if text.isdigit():
        return float(text)

    if text.replace(".", "", 1).isdigit() and text.count(".") == 1:
        return float(text)

    match = _TIMESTAMP_RE.match(text.replace(" ", ""))
    if match:
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        seconds = float(match.group("seconds") or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total > 0 or text.startswith("0"):
            return total

    raise ValueError(
        f"Could not parse timestamp '{value}'. "
        "Try formats like 7:30, 00:07:30, 7m30s, or 450."
    )


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    if seconds < 0:
        raise ValueError("Timestamp cannot be negative")
    whole = int(seconds)
    fraction = seconds - whole
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if fraction:
        return f"{hours:02d}:{minutes:02d}:{secs + fraction:06.3f}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_compact(seconds: float) -> str:
    """Compact timestamp for filenames, e.g. 000730."""
    whole = int(seconds)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}{minutes:02d}{secs:02d}"


def parse_clip_lines(text: str) -> list[tuple[float, float]]:
    """Parse multiple clip ranges from sheet-style text.

  Each non-empty line should be ``start - end``, for example::

      00:00 - 7:45
      10:00 - 12:00
      16:00 - 17:00
    """
    clips: list[tuple[float, float]] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = re.split(r"\s*(?:-|–|—|->|to|,|\t)\s*", line, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ValueError(
                f"Line {line_number}: use the format 'start - end' (e.g. 00:00 - 7:45)"
            )

        start_seconds = parse_timestamp(parts[0].strip())
        end_seconds = parse_timestamp(parts[1].strip())
        if end_seconds <= start_seconds:
            raise ValueError(f"Line {line_number}: end time must be after start time")
        clips.append((start_seconds, end_seconds))

    if not clips:
        raise ValueError("Add at least one clip line (start - end)")
    return clips

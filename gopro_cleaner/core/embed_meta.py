"""Embed GoPro Cleaner segments JSON inside an MP4 (no re-encode).

Appends a top-level ISO-BMFF ``skip`` box::

    [size:4][fourcc:"skip"][magic:"WCSG"][version:4][json utf-8...]

Players / ffmpeg / GoPro tools ignore ``skip`` boxes; video, audio and gpmd
(IMU) tracks are never touched. Same format as ``sd_offloader.embed_meta``.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

MAGIC = b"WCSG"  # World Context SeGments
VERSION = 1
_HEADER = 8


def build_box(payload: dict) -> bytes:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    body = MAGIC + struct.pack(">I", VERSION) + data
    return struct.pack(">I", _HEADER + len(body)) + b"skip" + body


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


def _find_embedded(handle, file_size: int) -> tuple[int, int] | None:
    found: tuple[int, int] | None = None
    for offset, size, fourcc in _walk_top_level(handle, file_size):
        if fourcc == b"skip" and size >= _HEADER + len(MAGIC):
            handle.seek(offset + _HEADER)
            if handle.read(len(MAGIC)) == MAGIC:
                found = (offset, size)
    return found


def embed_segments_json(mp4: Path, payload: dict) -> int:
    """Append (or replace trailing) WCSG box. Returns bytes written."""
    mp4 = Path(mp4)
    box = build_box(payload)
    file_size = mp4.stat().st_size
    with mp4.open("r+b") as handle:
        existing = _find_embedded(handle, file_size)
        if existing and existing[0] + existing[1] == file_size:
            handle.truncate(existing[0])
        handle.seek(0, 2)
        handle.write(box)
    return len(box)


def has_embedded_segments(mp4: Path) -> bool:
    """Fast presence check (does not parse the JSON payload)."""
    mp4 = Path(mp4)
    try:
        file_size = mp4.stat().st_size
    except OSError:
        return False
    with mp4.open("rb") as handle:
        return _find_embedded(handle, file_size) is not None


def read_embedded_segments(mp4: Path) -> dict | None:
    mp4 = Path(mp4)
    file_size = mp4.stat().st_size
    with mp4.open("rb") as handle:
        found = _find_embedded(handle, file_size)
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

"""Extract GoPro camera / IMU metadata from MP4 files.

Sources, in order of preference:
- ``moov/udta/GPMF`` box — camera serial (CASN), model (MINF), firmware (FMWR),
  media unique id (MUID). Legacy HERO3–5 ``udta`` atoms (FIRM/CAME/LENS) are
  also read.
- First second of the ``gpmd`` metadata track — names of the recorded sensor
  streams (Accelerometer, Gyroscope, GPS, …).
- ffprobe format/stream tags — recording timestamp, resolution, frame rate,
  codec.

Results are cached per (path, mtime, size); extraction only runs once per file.
"""

from __future__ import annotations

import struct
import subprocess
import threading
import time
from pathlib import Path

_CACHE_MAX = 512
_cache: dict[str, tuple[int, int, dict]] = {}
_cache_lock = threading.Lock()

# GPMF KLV type sizes we care about; container items have type 0.
_GPMF_NESTED = 0


def _read_atom_header(handle, offset: int, end: int) -> tuple[int, bytes, int] | None:
    """Return (payload_offset, fourcc, atom_end) for the atom at ``offset``."""
    if offset + 8 > end:
        return None
    handle.seek(offset)
    header = handle.read(8)
    if len(header) < 8:
        return None
    size = struct.unpack(">I", header[:4])[0]
    fourcc = header[4:8]
    payload = offset + 8
    if size == 1:  # 64-bit largesize
        big = handle.read(8)
        if len(big) < 8:
            return None
        size = struct.unpack(">Q", big)[0]
        payload = offset + 16
    elif size == 0:  # atom extends to end of file
        size = end - offset
    if size < 8 or offset + size > end:
        return None
    return payload, fourcc, offset + size


def _find_child_atom(handle, start: int, end: int, name: bytes) -> tuple[int, int] | None:
    """Scan a container's children for ``name``; return (payload_start, atom_end)."""
    offset = start
    while offset < end:
        parsed = _read_atom_header(handle, offset, end)
        if parsed is None:
            return None
        payload, fourcc, atom_end = parsed
        if fourcc == name:
            return payload, atom_end
        offset = atom_end
    return None


def _read_udta_boxes(path: Path) -> dict[bytes, bytes]:
    """Return raw payloads of interesting ``moov/udta`` child atoms.

    Scans every ``udta`` under ``moov`` — encoders may write their own ``udta``
    (e.g. an ffmpeg tool tag) alongside GoPro's.
    """
    wanted = {b"GPMF", b"FIRM", b"CAME", b"LENS", b"MUID"}
    out: dict[bytes, bytes] = {}
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        moov = _find_child_atom(handle, 0, file_size, b"moov")
        if not moov:
            return out
        child = moov[0]
        while child < moov[1]:
            parsed = _read_atom_header(handle, child, moov[1])
            if parsed is None:
                break
            udta_payload, fourcc, atom_end = parsed
            if fourcc == b"udta":
                offset = udta_payload
                while offset < atom_end:
                    inner = _read_atom_header(handle, offset, atom_end)
                    if inner is None:
                        break
                    payload, name, inner_end = inner
                    if name in wanted and name not in out and (inner_end - payload) <= 1_000_000:
                        handle.seek(payload)
                        out[name] = handle.read(inner_end - payload)
                    offset = inner_end
            child = atom_end
    return out


def _iter_gpmf(data: bytes, offset: int = 0, end: int | None = None, depth: int = 0):
    """Yield (fourcc, type_char, payload_bytes) for every GPMF KLV item."""
    if depth > 8:
        return
    end = len(data) if end is None else end
    while offset + 8 <= end:
        fourcc = data[offset : offset + 4]
        type_byte = data[offset + 4]
        struct_size = data[offset + 5]
        repeat = struct.unpack(">H", data[offset + 6 : offset + 8])[0]
        length = struct_size * repeat
        payload_start = offset + 8
        payload_end = payload_start + length
        if payload_end > end or not fourcc.isalnum():
            return
        payload = data[payload_start:payload_end]
        yield fourcc, type_byte, payload
        if type_byte == _GPMF_NESTED and length > 0:
            yield from _iter_gpmf(data, payload_start, payload_end, depth + 1)
        offset = payload_start + ((length + 3) // 4) * 4


def _gpmf_string(payload: bytes) -> str:
    return payload.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def _parse_gpmf_camera_fields(data: bytes) -> dict:
    """Pull camera identity fields out of a GPMF blob (udta GPMF box)."""
    out: dict = {}
    for fourcc, type_byte, payload in _iter_gpmf(data):
        if fourcc == b"CASN" and "camera_serial" not in out:
            out["camera_serial"] = _gpmf_string(payload)
        elif fourcc == b"MINF" and "camera_model" not in out:
            out["camera_model"] = _gpmf_string(payload)
        elif fourcc == b"FMWR" and "firmware" not in out:
            out["firmware"] = _gpmf_string(payload)
        elif fourcc == b"MUID" and "media_uid" not in out and len(payload) >= 4:
            words = struct.unpack(f">{len(payload) // 4}I", payload[: (len(payload) // 4) * 4])
            trimmed = [w for w in words if w]
            if trimmed:
                out["media_uid"] = "".join(f"{w:08x}" for w in words[:8]).rstrip("0") or None
    return {k: v for k, v in out.items() if v}


def _parse_sensor_names(gpmd_sample: bytes) -> list[str]:
    """Collect unique STNM stream names from raw gpmd track data."""
    names: list[str] = []
    for fourcc, _type, payload in _iter_gpmf(gpmd_sample):
        if fourcc == b"STNM":
            name = _gpmf_string(payload)
            if name and name not in names:
                names.append(name)
    return names


def _dump_gpmd_head(path: Path, stream_index: int, seconds: float = 1.0) -> bytes:
    """Copy the first ``seconds`` of the gpmd track without re-encoding."""
    from .ffmpeg_tools import ffmpeg_bin

    result = subprocess.run(
        [
            ffmpeg_bin(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-t",
            str(seconds),
            "-i",
            str(path),
            "-map",
            f"0:{stream_index}",
            "-c",
            "copy",
            "-f",
            "data",
            "-",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return result.stdout if result.returncode == 0 else b""


def _ffprobe_fields(path: Path) -> dict:
    from .probe import _run_ffprobe

    payload = _run_ffprobe(path)
    fmt = payload.get("format", {}) or {}
    tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}

    out: dict = {}
    if tags.get("creation_time"):
        out["recorded_at"] = str(tags["creation_time"])
    if tags.get("firmware"):
        out["firmware"] = str(tags["firmware"])
    if tags.get("location"):
        out["location"] = str(tags["location"])

    for stream in payload.get("streams", []) or []:
        if stream.get("codec_type") != "video":
            continue
        out["width"] = stream.get("width")
        out["height"] = stream.get("height")
        out["video_codec"] = stream.get("codec_name")
        rate = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "")
        if "/" in rate:
            num, _, den = rate.partition("/")
            try:
                if float(den) > 0:
                    out["fps"] = round(float(num) / float(den), 3)
            except ValueError:
                pass
        break
    return out


def get_media_meta(path: Path) -> dict:
    """Camera + IMU metadata for a video file. Never raises; returns {} fields on failure."""
    path = Path(path).expanduser().resolve()
    stat = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == stat.st_mtime_ns and hit[1] == stat.st_size:
            return dict(hit[2])

    meta: dict = {}

    try:
        meta.update(_ffprobe_fields(path))
    except Exception:  # noqa: BLE001
        pass

    try:
        boxes = _read_udta_boxes(path)
        if b"GPMF" in boxes:
            for field, value in _parse_gpmf_camera_fields(boxes[b"GPMF"]).items():
                meta.setdefault(field, value)
        # Legacy HERO3–5 atoms
        if b"FIRM" in boxes and not meta.get("firmware"):
            meta["firmware"] = _gpmf_string(boxes[b"FIRM"])
        if b"CAME" in boxes and not meta.get("camera_serial"):
            meta["camera_serial"] = boxes[b"CAME"].hex()
        if b"LENS" in boxes:
            meta.setdefault("lens_serial", _gpmf_string(boxes[b"LENS"]))
    except Exception:  # noqa: BLE001
        pass

    try:
        from .probe import probe_media

        info = probe_media(path)
        meta["has_gpmf"] = info.has_gpmf
        if info.gpmf_index is not None:
            sample = _dump_gpmd_head(path, info.gpmf_index)
            if sample:
                sensors = _parse_sensor_names(sample)
                if sensors:
                    meta["sensors"] = sensors
    except Exception:  # noqa: BLE001
        meta.setdefault("has_gpmf", False)

    meta["extracted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (stat.st_mtime_ns, stat.st_size, dict(meta))
    return meta

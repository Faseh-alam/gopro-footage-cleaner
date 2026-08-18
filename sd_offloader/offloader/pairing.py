"""Sidecar ↔ MP4 identity checks and batch dedupe helpers."""

from __future__ import annotations

import json
from pathlib import Path

from . import embed_meta


def load_sidecar(path: str | Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def validate_sidecar_for_mp4(
    mp4_path: Path,
    sidecar_path: Path,
    payload: dict | None = None,
) -> list[str]:
    """Return errors when sidecar metadata does not belong to this MP4."""
    mp4_path = Path(mp4_path)
    sidecar_path = Path(sidecar_path)
    if payload is None:
        payload = load_sidecar(sidecar_path)
    if not payload:
        return ["sidecar unreadable or not a JSON object"]

    errors: list[str] = []
    expected_name = mp4_path.name
    source = str(payload.get("source") or "").strip()
    if source and source != expected_name:
        errors.append(f"sidecar source '{source}' does not match MP4 '{expected_name}'")

    expected_sidecar = mp4_path.with_name(f"{mp4_path.stem}.segments.json")
    if sidecar_path.name.lower().endswith(".segments.json"):
        if sidecar_path.name.lower() != expected_sidecar.name.lower():
            errors.append(
                f"sidecar filename '{sidecar_path.name}' does not match MP4 stem '{mp4_path.stem}'"
            )

    try:
        actual_size = mp4_path.stat().st_size
    except OSError:
        errors.append(f"cannot read MP4 size for {expected_name}")
        return errors

    recorded_size = payload.get("size_bytes")
    if recorded_size is not None:
        try:
            if int(recorded_size) != int(actual_size):
                errors.append(
                    f"sidecar size_bytes {recorded_size} != MP4 size {actual_size}"
                )
        except (TypeError, ValueError):
            errors.append("sidecar size_bytes is not a valid integer")

    return errors


def payloads_equivalent(a: dict, b: dict) -> bool:
    """True when two payloads describe the same source video identity."""
    keys = ("source", "size_bytes", "device_id", "device_type", "card_badge")
    for key in keys:
        av = a.get(key)
        bv = b.get(key)
        if av is None and bv is None:
            continue
        if str(av or "") != str(bv or ""):
            return False
    meta_a = a.get("media_meta") or {}
    meta_b = b.get("media_meta") or {}
    for key in ("camera_serial", "recorded_at"):
        av = meta_a.get(key)
        bv = meta_b.get(key)
        if av is None and bv is None:
            continue
        if str(av or "") != str(bv or ""):
            return False
    return True


def batch_file_is_same_video(
    dest_mp4: Path,
    *,
    card_mp4_size: int,
    sidecar: dict,
) -> bool:
    """True when the SSD already holds this exact card video."""
    dest_mp4 = Path(dest_mp4)
    if not dest_mp4.is_file():
        return False

    if sidecar.get("size_bytes") is not None:
        try:
            if int(sidecar["size_bytes"]) != int(card_mp4_size):
                return False
        except (TypeError, ValueError):
            return False

    embedded = embed_meta.read_embedded_segments(dest_mp4)
    if embedded:
        return payloads_equivalent(embedded, sidecar)

    try:
        return dest_mp4.stat().st_size == int(card_mp4_size)
    except OSError:
        return False


def find_existing_dest_name(
    dest: Path,
    mp4_name: str,
    *,
    card_mp4_size: int,
    sidecar: dict | None,
) -> str | None:
    """Return the batch-relative MP4 name when this video is already stored."""
    candidate = dest / mp4_name
    if not candidate.is_file():
        return None
    if sidecar and batch_file_is_same_video(
        candidate, card_mp4_size=card_mp4_size, sidecar=sidecar
    ):
        return mp4_name
    if not sidecar:
        try:
            if candidate.stat().st_size == int(card_mp4_size):
                return mp4_name
        except OSError:
            return None
    return None

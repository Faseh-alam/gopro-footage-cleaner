"""Sidecar ↔ MP4 identity checks and batch dedupe helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import embed_meta

_HASH_CHUNK = 1024 * 1024


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


def dest_identity_unclear(dest_mp4: Path) -> bool:
    """True when the SSD copy has no embed/sidecar we can compare."""
    dest_mp4 = Path(dest_mp4)
    if not dest_mp4.is_file():
        return True
    if embed_meta.read_embedded_segments(dest_mp4):
        return False
    from . import inventory as _inventory

    dest_side = _inventory.sidecar_for_mp4(dest_mp4)
    return load_sidecar(dest_side) is None if dest_side else True


def file_digest(path: Path) -> str | None:
    """SHA-256 of a file. Used only when metadata cannot decide identity."""
    path = Path(path)
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def files_same_bytes(left: Path, right: Path) -> bool:
    """Size first, then hash. Never hashes when sizes differ."""
    left, right = Path(left), Path(right)
    try:
        if int(left.stat().st_size) != int(right.stat().st_size):
            return False
    except OSError:
        return False
    a = file_digest(left)
    b = file_digest(right)
    return bool(a and b and a == b)


def is_name_variant(original_stem: str, other_stem: str) -> bool:
    """``GX010001``, ``GX010001-1``, and legacy ``GX010001__C1234`` are variants."""
    original_stem = str(original_stem or "")
    other_stem = str(other_stem or "")
    if not original_stem or not other_stem:
        return False
    if other_stem == original_stem:
        return True
    if other_stem.startswith(f"{original_stem}-") and other_stem[len(original_stem) + 1 :].isdigit():
        return True
    if other_stem.startswith(f"{original_stem}__"):
        return True
    return False


def batch_file_is_same_video(
    dest_mp4: Path,
    *,
    card_mp4_size: int,
    sidecar: dict,
) -> bool:
    """True when the SSD already holds this exact card video (identity, not name)."""
    dest_mp4 = Path(dest_mp4)
    if not dest_mp4.is_file() or not sidecar:
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

    from . import inventory as _inventory

    dest_side = _inventory.sidecar_for_mp4(dest_mp4)
    dest_payload = load_sidecar(dest_side) if dest_side else None
    if dest_payload:
        return payloads_equivalent(dest_payload, sidecar)
    return False


def find_existing_dest_name(
    dest: Path,
    mp4_name: str,
    *,
    card_mp4_size: int,
    sidecar: dict | None,
    card_mp4: Path | str | None = None,
) -> str | None:
    """Reuse a batch MP4 only when it is the same video — never overwrite.

    Fast path: sidecar/embed identity. Hash only when identity is unclear
    (missing metadata) and sizes already match.
    """
    dest = Path(dest)
    mp4_name = Path(str(mp4_name)).name
    stem = Path(mp4_name).stem
    suffix = Path(mp4_name).suffix.lower()
    card_path = Path(card_mp4) if card_mp4 else None
    candidates: list[Path] = []
    try:
        for path in dest.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() != suffix:
                continue
            if is_name_variant(stem, path.stem):
                candidates.append(path)
    except OSError:
        pass
    exact = dest / mp4_name
    if exact.is_file() and exact not in candidates:
        candidates.insert(0, exact)

    if sidecar:
        for path in candidates:
            if batch_file_is_same_video(
                path, card_mp4_size=card_mp4_size, sidecar=sidecar
            ):
                return path.name
        if card_path and card_path.is_file():
            for path in candidates:
                if not dest_identity_unclear(path):
                    continue
                if files_same_bytes(path, card_path):
                    return path.name
        return None

    if card_path and card_path.is_file():
        for path in candidates:
            if files_same_bytes(path, card_path):
                return path.name
    return None

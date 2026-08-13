"""Discover transferable MP4s + .segments.json sidecars under DCIM/xxxGOPRO."""

from __future__ import annotations

from pathlib import Path

from .detect import _find_gopro_root


def list_transfer_files(card_root: Path) -> list[dict]:
    """Return MP4 + sidecar files to copy from the GOPRO root.

    Layout (Review Station labeling — videos stay untrimmed on the card)::

        DCIM/###GOPRO/
          GX010001.MP4
          GX010001.segments.json
    """
    gopro = _find_gopro_root(card_root)
    if gopro is None:
        return []

    try:
        entries = list(gopro.iterdir())
    except OSError:
        return []

    root_files = [p for p in entries if p.is_file() and not p.name.startswith("._")]
    files: list[dict] = []

    for item in sorted(root_files, key=lambda p: p.name.lower()):
        is_mp4 = item.suffix.upper() == ".MP4"
        is_sidecar = item.name.lower().endswith(".segments.json")
        if not is_mp4 and not is_sidecar:
            continue
        try:
            size = item.stat().st_size
        except OSError:
            continue
        row = {
            "rel": item.name,
            "source": str(item.resolve()),
            "size": size,
            "task": "",
        }
        if is_mp4:
            sidecar = item.with_name(f"{item.stem}.segments.json")
            if sidecar.is_file():
                row["embed_json"] = str(sidecar.resolve())
        files.append(row)

    return files


def total_bytes(files: list[dict]) -> int:
    return sum(int(f.get("size") or 0) for f in files)

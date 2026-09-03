"""Per-video output folders under recordings/."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RECORDINGS = ROOT / "recordings"
STATIC = ROOT / "static"
SOURCE_DIR = RECORDINGS / "source"

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_stem(name: str) -> str:
    stem = Path(name or "video").stem
    cleaned = SAFE_NAME.sub("_", stem).strip("._")
    return cleaned or "video"


def safe_filename(name: str) -> str:
    cleaned = SAFE_NAME.sub("_", Path(name or "video").name).strip("._")
    return cleaned or "video.mp4"


def video_dir(video_name: str, *, mkdir: bool = True) -> Path:
    folder = RECORDINGS / safe_stem(video_name)
    if mkdir:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def sidecar_path(video_name: str) -> Path:
    stem = safe_stem(video_name)
    nested = RECORDINGS / stem / f"{stem}.takes.json"
    if nested.is_file():
        return nested
    flat = RECORDINGS / f"{stem}.takes.json"
    if flat.is_file():
        return flat
    return nested


def in_video_dir(video_name: str, filename: str) -> Path:
    return video_dir(video_name) / Path(filename).name


def resolve_media(video_name: str, stored: str | None) -> Path | None:
    if not stored:
        return None
    path = Path(stored)
    if path.is_file():
        return path
    nested = video_dir(video_name, mkdir=False) / path.name
    if nested.is_file():
        return nested
    flat = RECORDINGS / path.name
    if flat.is_file():
        return flat
    source = SOURCE_DIR / path.name
    if source.is_file():
        return source
    return None


def migrate_flat_recordings() -> None:
    """Move older root-level takes into recordings/<video-name>/."""
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    for json_path in list(RECORDINGS.glob("*.takes.json")):
        if json_path.parent.resolve() != RECORDINGS.resolve():
            continue
        stem = json_path.name[: -len(".takes.json")]
        dest = RECORDINGS / stem
        dest.mkdir(parents=True, exist_ok=True)
        target_json = dest / json_path.name
        if json_path.resolve() != target_json.resolve():
            shutil.move(str(json_path), str(target_json))
        for item in list(RECORDINGS.iterdir()):
            if not item.is_file():
                continue
            if item.name == f"{stem}.mp4" or item.name.startswith(f"{stem}_"):
                shutil.move(str(item), str(dest / item.name))
        if SOURCE_DIR.is_dir():
            for item in list(SOURCE_DIR.iterdir()):
                if item.is_file() and (item.stem == stem or item.name.startswith(f"{stem}.")):
                    shutil.move(str(item), str(dest / item.name))
        try:
            data = json.loads(target_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["folder"] = stem
        for take in data.get("takes") or []:
            take["audio"] = Path(take.get("audio") or "").name
        export_name = Path(data.get("export") or f"{stem}_voiceover.mp4").name
        if (dest / export_name).is_file():
            data["export"] = export_name
        source_mp4 = next(
            (
                p.name
                for p in dest.glob("*.mp4")
                if p.name != export_name and "_take" not in p.name and not p.name.endswith("_voiceover.mp4")
            ),
            None,
        )
        if source_mp4:
            data["source"] = source_mp4
        target_json.write_text(json.dumps(data, indent=2), encoding="utf-8")

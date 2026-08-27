"""ScaleAI micro-task stitching — concatenate short GoPro clips with GPMF.

Each micro-task (e.g. ``grab-cloth``) may be marked dozens of times as sub-second
clips. This module joins those clips into one delivery MP4 while stream-copying
video/audio and the GoPro ``gpmd`` IMU track.

Notes for operators / ScaleAI:
- IMU samples are preserved per segment (stream copy). Absolute camera time is
  not continuous across joins; each clip's GPMF payload is appended in order.
- All input clips for one stitch must share compatible codecs (same GoPro
  settings). Mixed resolutions/codecs will fail closed.
- Prefer delivering the stitched file *and* keeping individual clips until
  ScaleAI confirms acceptance.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .ffmpeg_tools import ffmpeg_bin
from .probe import MediaInfo, is_video_file, probe_media
from .trimmer import find_udtacopy

VIDEO_EXTENSIONS = {".mp4", ".mov", ".MP4", ".MOV"}


@dataclass
class StitchPlan:
    task: str
    clips: list[Path]
    output: Path
    total_duration: float = 0.0
    clip_count: int = 0
    all_have_gpmf: bool = False


@dataclass
class StitchResult:
    ok: bool
    task: str
    output: str
    clip_count: int
    duration: float | None
    has_gpmf: bool
    message: str = ""
    error: str | None = None
    manifest: dict = field(default_factory=dict)


_lock = threading.Lock()


def list_task_clips(task_dir: Path) -> list[Path]:
    """Sorted MP4s directly inside a task folder (skip nested / stitched outputs)."""
    root = task_dir.expanduser().resolve()
    if not root.is_dir():
        return []
    clips: list[Path] = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".mp4", ".mov"}:
            continue
        # Skip previously exported stitch packs and partials.
        name = path.name.lower()
        if name.endswith(".partial") or name.startswith("."):
            continue
        if "__stitched" in name or name.startswith("stitched-"):
            continue
        clips.append(path)
    return clips


def _probe_or_raise(path: Path) -> MediaInfo:
    media = probe_media(path)
    if media.video_index is None:
        raise RuntimeError(f"No video stream: {path.name}")
    return media


def _compatible(a: MediaInfo, b: MediaInfo) -> list[str]:
    """Return human-readable incompatibilities (empty = OK)."""
    errors: list[str] = []
    av = next((s for s in a.streams if s.index == a.video_index), None)
    bv = next((s for s in b.streams if s.index == b.video_index), None)
    if av and bv and (av.codec_name or "") != (bv.codec_name or ""):
        errors.append(
            f"video codec mismatch {a.path.name}={av.codec_name} vs {b.path.name}={bv.codec_name}"
        )
    if (a.audio_index is None) != (b.audio_index is None):
        errors.append(f"audio presence mismatch between {a.path.name} and {b.path.name}")
    if (a.gpmf_index is None) != (b.gpmf_index is None):
        errors.append(f"GPMF presence mismatch between {a.path.name} and {b.path.name}")
    return errors


def plan_stitch(
    task_dir: Path,
    *,
    task_name: str | None = None,
    output: Path | None = None,
    clips: list[Path] | None = None,
) -> StitchPlan:
    root = task_dir.expanduser().resolve()
    task = (task_name or root.name).strip() or "task"
    selected = [Path(c).expanduser().resolve() for c in (clips or list_task_clips(root))]
    if not selected:
        raise ValueError(f"No clips found in {root}")

    probed = [_probe_or_raise(p) for p in selected]
    base = probed[0]
    for other in probed[1:]:
        mismatches = _compatible(base, other)
        if mismatches:
            raise RuntimeError("; ".join(mismatches))

    total = sum(float(m.duration or 0.0) for m in probed)
    out = output
    if out is None:
        out = root / f"{task}__stitched.MP4"
    else:
        out = Path(out).expanduser().resolve()

    return StitchPlan(
        task=task,
        clips=selected,
        output=out,
        total_duration=total,
        clip_count=len(selected),
        all_have_gpmf=all(m.has_gpmf for m in probed),
    )


def _build_concat_command(first: MediaInfo, list_file: Path, output: Path) -> list[str]:
    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-map",
        f"0:{first.video_index}",
    ]
    if first.audio_index is not None:
        command.extend(["-map", f"0:{first.audio_index}"])
    if first.gpmf_index is not None:
        data_tag_index = 2 if first.audio_index is not None else 1
        command.extend(
            [
                "-map",
                f"0:{first.gpmf_index}",
                "-copy_unknown",
                f"-tag:d:{data_tag_index}",
                "gpmd",
            ]
        )
    command.extend(["-c", "copy", str(output)])
    return command


def _write_concat_list(clips: list[Path], list_file: Path) -> None:
    lines = []
    for clip in clips:
        # ffmpeg concat list requires escaped single quotes in paths.
        escaped = str(clip).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_udtacopy(source: Path, target: Path) -> None:
    tool = find_udtacopy()
    if tool is None:
        return
    subprocess.run([str(tool), str(source), str(target)], check=True)


def stitch_task_clips(
    task_dir: Path,
    *,
    task_name: str | None = None,
    output: Path | None = None,
    clips: list[Path] | None = None,
    overwrite: bool = False,
) -> StitchResult:
    """Concatenate all clips in a task folder into one MP4 with GPMF mapped."""
    with _lock:
        plan = plan_stitch(task_dir, task_name=task_name, output=output, clips=clips)
        if plan.output.exists() and not overwrite:
            raise FileExistsError(f"Stitched output already exists: {plan.output}")

        plan.output.parent.mkdir(parents=True, exist_ok=True)
        first = _probe_or_raise(plan.clips[0])

        partial = plan.output.with_suffix(plan.output.suffix + ".partial")
        if partial.exists():
            partial.unlink()

        with tempfile.TemporaryDirectory(prefix="scaleai-stitch-") as tmp:
            list_file = Path(tmp) / "concat.txt"
            _write_concat_list(plan.clips, list_file)
            command = _build_concat_command(first, list_file, partial)
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "ffmpeg concat failed").strip()
                if partial.exists():
                    partial.unlink(missing_ok=True)
                return StitchResult(
                    ok=False,
                    task=plan.task,
                    output=str(plan.output),
                    clip_count=plan.clip_count,
                    duration=None,
                    has_gpmf=False,
                    error=err,
                )

            # Best-effort GoPro header restore from the first clip.
            try:
                _run_udtacopy(plan.clips[0], partial)
            except Exception:  # noqa: BLE001
                pass

            if plan.output.exists():
                plan.output.unlink()
            partial.replace(plan.output)

        trimmed = probe_media(plan.output)
        if plan.all_have_gpmf and not trimmed.has_gpmf:
            return StitchResult(
                ok=False,
                task=plan.task,
                output=str(plan.output),
                clip_count=plan.clip_count,
                duration=trimmed.duration,
                has_gpmf=False,
                error="Stitched file is missing GPMF / IMU track — refusing to mark success",
            )

        clip_rows = []
        for p in plan.clips:
            info = probe_media(p)
            clip_rows.append(
                {
                    "path": str(p),
                    "name": p.name,
                    "duration": info.duration,
                    "has_gpmf": info.has_gpmf,
                }
            )
        manifest = {
            "task": plan.task,
            "output": str(plan.output),
            "clip_count": plan.clip_count,
            "clips": clip_rows,
            "stitched_duration": trimmed.duration,
            "has_gpmf": trimmed.has_gpmf,
        }
        manifest_path = plan.output.with_suffix(".manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        return StitchResult(
            ok=True,
            task=plan.task,
            output=str(plan.output),
            clip_count=plan.clip_count,
            duration=trimmed.duration,
            has_gpmf=trimmed.has_gpmf,
            message=f"Stitched {plan.clip_count} clips → {plan.output.name}",
            manifest=manifest,
        )


def discover_task_dirs(root: Path) -> list[Path]:
    """Find immediate child folders that contain MP4 clips (task folders)."""
    root = root.expanduser().resolve()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if list_task_clips(child):
            found.append(child)
    return found


def stitch_all_tasks(
    root: Path,
    *,
    overwrite: bool = False,
) -> list[StitchResult]:
    """Stitch every task folder under ``root`` (e.g. a DCIM/###GOPRO tree)."""
    results: list[StitchResult] = []
    for task_dir in discover_task_dirs(root):
        try:
            results.append(stitch_task_clips(task_dir, overwrite=overwrite))
        except Exception as exc:  # noqa: BLE001
            results.append(
                StitchResult(
                    ok=False,
                    task=task_dir.name,
                    output="",
                    clip_count=0,
                    duration=None,
                    has_gpmf=False,
                    error=str(exc),
                )
            )
    return results

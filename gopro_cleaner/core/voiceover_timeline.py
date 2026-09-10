"""Pause-aware narration timeline: freeze frames during SPACE pauses.

Microphone uses real elapsed session time. Video play/pause events are logged
with (session_t, video_t). Export rebuilds video so each pause becomes a
freeze-frame of the same duration, then muxes narrator audio as the only track.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ffmpeg_tools import ffmpeg_bin
from .probe import MediaInfo, probe_media

MIN_DURATION_S = 60.0
MAX_DURATION_S = 1200.0
MIN_HEIGHT = 1080


@dataclass
class TimelineSegment:
    kind: str  # "play" | "freeze"
    duration: float
    source_start: float = 0.0
    source_end: float = 0.0
    freeze_at: float = 0.0


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def build_segments_from_events(
    events: list[dict],
    *,
    source_duration: float,
    session_end: float | None = None,
) -> list[TimelineSegment]:
    """Turn play/pause/resume/stop events into play + freeze segments."""
    source_duration = max(0.0, float(source_duration or 0.0))
    if not events:
        # No log — treat whole session as continuous play (legacy).
        end = session_end if session_end and session_end > 0 else source_duration
        end = min(end, source_duration) if source_duration > 0 else end
        if end <= 0:
            return []
        return [TimelineSegment("play", end, 0.0, end)]

    ordered = sorted(events, key=lambda e: _f(e.get("session_t")))
    segments: list[TimelineSegment] = []
    last_session = 0.0
    last_video = 0.0
    # Start playing once we see the first play/resume, else assume playing.
    state = "playing"
    first = str(ordered[0].get("type") or "").lower()
    if first in {"pause"}:
        state = "paused"

    def flush(until_session: float) -> None:
        nonlocal last_session, last_video
        dt = until_session - last_session
        if dt <= 0.001:
            return
        if state == "playing":
            if source_duration > 0:
                v_end = min(source_duration, last_video + dt)
            else:
                v_end = last_video + dt
            play_dur = max(0.0, v_end - last_video)
            if play_dur > 0.001:
                segments.append(
                    TimelineSegment("play", play_dur, last_video, v_end)
                )
            freeze_extra = dt - play_dur
            if freeze_extra > 0.001:
                freeze_at = v_end if source_duration <= 0 else min(v_end, source_duration)
                # If we already hit the end, freeze the last frame.
                if source_duration > 0:
                    freeze_at = min(freeze_at, max(0.0, source_duration - 1 / 30))
                segments.append(TimelineSegment("freeze", freeze_extra, freeze_at=freeze_at))
            last_video = v_end
        else:
            freeze_at = last_video
            if source_duration > 0:
                freeze_at = min(freeze_at, max(0.0, source_duration - 1 / 30))
            segments.append(TimelineSegment("freeze", dt, freeze_at=freeze_at))
        last_session = until_session

    for raw in ordered:
        etype = str(raw.get("type") or "").lower()
        session_t = max(0.0, _f(raw.get("session_t")))
        video_t = max(0.0, _f(raw.get("video_t"), last_video))
        if source_duration > 0:
            video_t = min(video_t, source_duration)

        flush(session_t)

        if etype == "pause":
            state = "paused"
            last_video = video_t
        elif etype in {"play", "resume"}:
            state = "playing"
            last_video = video_t
        elif etype == "stop":
            state = "stopped"
            last_video = video_t
            break

    if session_end is not None and session_end > last_session + 0.001 and state != "stopped":
        flush(float(session_end))

    # Merge tiny adjacent freezes / drop empty.
    merged: list[TimelineSegment] = []
    for seg in segments:
        if seg.duration <= 0.001:
            continue
        if (
            merged
            and merged[-1].kind == "freeze"
            and seg.kind == "freeze"
            and abs(merged[-1].freeze_at - seg.freeze_at) < 0.05
        ):
            merged[-1] = TimelineSegment(
                "freeze",
                merged[-1].duration + seg.duration,
                freeze_at=merged[-1].freeze_at,
            )
        else:
            merged.append(seg)
    return merged


def segments_total_duration(segments: list[TimelineSegment]) -> float:
    return sum(s.duration for s in segments)


def _rotation_filters(rotation: int | None) -> list[str]:
    """Normalize display rotation into pixels and clear metadata later."""
    rot = int(rotation or 0) % 360
    if rot == 90:
        return ["transpose=1"]
    if rot == 180:
        return ["transpose=1,transpose=1"]
    if rot == 270:
        return ["transpose=2"]
    return []


def build_freeze_export_command(
    *,
    source: Path,
    audio_path: Path,
    output: Path,
    segments: list[TimelineSegment],
    media: MediaInfo,
    fps: float = 30.0,
) -> list[str]:
    if not segments:
        raise RuntimeError("No timeline segments to export")
    total = segments_total_duration(segments)
    if total <= 0.05:
        raise RuntimeError("Export timeline is empty")

    fps = max(1.0, float(fps))
    rot = _rotation_filters(media.rotation)
    # Keep at least 1080p when source is >= 1080p (scale up only if needed? Prompt:
    # "be at least 1080p if the source is at least 1080p" — preserve source size).
    height = int(media.height or 0)
    width = int(media.width or 0)
    scale_parts: list[str] = []
    if height > 0 and width > 0:
        # Even dimensions for yuv420p.
        scale_parts = [f"scale=trunc(iw/2)*2:trunc(ih/2)*2"]

    filters: list[str] = []
    labels: list[str] = []
    for i, seg in enumerate(segments):
        base = "[0:v]" + "".join(f",{p}" for p in rot)
        if seg.kind == "play":
            start = max(0.0, seg.source_start)
            end = max(start + 0.001, seg.source_end)
            chain = (
                f"{base}trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,"
                f"fps={fps:.6f}"
            )
        else:
            at = max(0.0, seg.freeze_at)
            # One frame, then loop for the pause duration.
            frame_end = at + (1.0 / fps)
            if media.duration and frame_end > float(media.duration):
                at = max(0.0, float(media.duration) - 1.0 / fps)
                frame_end = min(float(media.duration), at + 1.0 / fps)
            chain = (
                f"{base}trim=start={at:.6f}:end={frame_end:.6f},setpts=PTS-STARTPTS,"
                f"loop=loop=-1:size=1:start=0,setpts=N/{fps:.6f}/TB,"
                f"trim=duration={seg.duration:.6f},fps={fps:.6f}"
            )
        if scale_parts:
            chain += "," + ",".join(scale_parts)
        chain += f"[v{i}]"
        filters.append(chain)
        labels.append(f"[v{i}]")

    n = len(segments)
    filters.append(f"{''.join(labels)}concat=n={n}:v=1:a=0[vout]")
    # Narration only — pad/trim to rebuilt video length.
    filters.append(
        f"[1:a:0]aformat=sample_rates=48000:channel_layouts=mono,"
        f"apad=whole_dur={total:.6f},atrim=0:{total:.6f}[aout]"
    )
    filter_complex = ";".join(filters)

    return [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-i",
        str(source),
        "-i",
        str(audio_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-t",
        f"{total:.6f}",
        "-metadata:s:v:0",
        "rotate=0",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(output),
    ]


def convert_audio_to_wav(audio_path: Path, wav_path: Path) -> Path:
    """Write mono PCM WAV @ 48 kHz beside the take."""
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(audio_path),
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not wav_path.is_file():
        err = (result.stderr or result.stdout or "wav convert failed").strip()
        raise RuntimeError(f"Could not write narration WAV: {err[:400]}")
    return wav_path


def validate_export_mp4(path: Path, *, source_height: int | None = None) -> dict:
    """ffprobe PASS/FAIL checks for Lightly P0 sample delivery."""
    media = probe_media(path)
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    suffix = path.suffix.lower()
    add("container_mp4", suffix == ".mp4", f"suffix={path.suffix}")

    vcodec = (media.video_codec or "").lower()
    add("video_h264", vcodec in {"h264", "avc1"}, f"codec={media.video_codec}")

    audio_n = int(media.audio_stream_count or (1 if media.audio_index is not None else 0))
    add("one_audio_stream", audio_n == 1, f"audio_streams={audio_n}")

    rot = int(media.rotation or 0) % 360
    add("no_rotation", rot == 0, f"rotation={media.rotation}")

    dur = float(media.duration or 0)
    add(
        "duration_60_1200",
        MIN_DURATION_S <= dur <= MAX_DURATION_S,
        f"duration={dur:.3f}s",
    )

    h = int(media.height or 0)
    need_1080 = (source_height or 0) >= MIN_HEIGHT or h >= MIN_HEIGHT
    if need_1080:
        add("resolution_1080p", h >= MIN_HEIGHT, f"{media.width}x{media.height}")
    else:
        add("resolution_1080p", True, f"{media.width}x{media.height} (source below 1080p)")

    passed = all(c["ok"] for c in checks)
    return {
        "ok": passed,
        "pass": passed,
        "checks": checks,
        "duration": dur,
        "width": media.width,
        "height": media.height,
        "video_codec": media.video_codec,
        "rotation": media.rotation,
        "audio_stream_count": audio_n,
        "path": str(path),
    }


def export_pause_aware(
    source: Path,
    audio_path: Path,
    *,
    events: list[dict],
    session_end: float | None = None,
    output: Path | None = None,
    save_wav: bool = True,
    save_events: bool = True,
) -> dict:
    """Rebuild timeline with freezes, write sibling MP4 (+ wav + events). Does not touch source."""
    source = source.expanduser().resolve(strict=True)
    audio_path = audio_path.expanduser().resolve(strict=True)
    media = probe_media(source)
    source_dur = float(media.duration or 0)
    segments = build_segments_from_events(
        events,
        source_duration=source_dur,
        session_end=session_end,
    )
    total = segments_total_duration(segments)
    if total < MIN_DURATION_S:
        raise RuntimeError(
            f"Projected export is only {total:.1f}s — need at least {MIN_DURATION_S:.0f}s"
        )
    if total > MAX_DURATION_S:
        raise RuntimeError(
            f"Projected export is {total:.1f}s — max allowed is {MAX_DURATION_S:.0f}s"
        )

    out = output or source.with_name(f"{source.stem}.narrated.mp4")
    out = out.expanduser().resolve()
    events_path = source.with_name(f"{source.stem}.session_events.json")
    wav_path = source.with_name(f"{source.stem}.narration.wav")

    work = Path(tempfile.mkdtemp(prefix="vo-timeline-"))
    partial = work / "out.mp4"
    try:
        cmd = build_freeze_export_command(
            source=source,
            audio_path=audio_path,
            output=partial,
            segments=segments,
            media=media,
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0 or not partial.is_file():
            err = (result.stderr or result.stdout or "ffmpeg export failed").strip()
            raise RuntimeError(err[:800])

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            out.unlink()
        partial.replace(out)

        if save_wav:
            convert_audio_to_wav(audio_path, wav_path)
        if save_events:
            payload = {
                "version": 1,
                "source_path": str(source),
                "source_duration": source_dur,
                "session_end": session_end,
                "output_path": str(out),
                "events": events,
                "segments": [
                    {
                        "kind": s.kind,
                        "duration": round(s.duration, 6),
                        "source_start": round(s.source_start, 6),
                        "source_end": round(s.source_end, 6),
                        "freeze_at": round(s.freeze_at, 6),
                    }
                    for s in segments
                ],
                "timeline_duration": round(total, 6),
            }
            events_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        validation = validate_export_mp4(out, source_height=media.height)
        return {
            "ok": True,
            "path": str(out),
            "source_path": str(source),
            "source_unchanged": True,
            "wav_path": str(wav_path) if save_wav and wav_path.is_file() else None,
            "events_path": str(events_path) if save_events and events_path.is_file() else None,
            "timeline_duration": total,
            "segment_count": len(segments),
            "validation": validation,
            "message": (
                f"Exported narrated MP4 (freeze-frame pauses) → {out.name}. "
                f"Source left unchanged."
            ),
        }
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)

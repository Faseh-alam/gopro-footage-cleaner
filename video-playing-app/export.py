"""Build a voiceover MP4: picture + mic, freeze-frame while the video was paused."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from paths import STATIC, resolve_media, safe_stem, video_dir


def ffprobe_json(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe failed on {path.name}")
    return json.loads(result.stdout or "{}")


def stream_fps(path: Path) -> float:
    data = ffprobe_json(path)
    for stream in data.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "30/1"
        if "/" in rate:
            num, den = rate.split("/", 1)
            try:
                return max(1.0, float(num) / max(float(den), 1.0))
            except ValueError:
                return 30.0
        try:
            return max(1.0, float(rate))
        except ValueError:
            return 30.0
    return 30.0


def media_duration(path: Path) -> float:
    data = ffprobe_json(path)
    fmt = data.get("format") or {}
    try:
        return max(0.0, float(fmt.get("duration") or 0))
    except (TypeError, ValueError):
        return 0.0


def find_source(video_name: str, sidecar: dict) -> Path:
    found = resolve_media(video_name, sidecar.get("source"))
    if found:
        return found
    folder = video_dir(video_name, mkdir=False)
    if folder.is_dir():
        for cand in folder.glob("*.mp4"):
            if cand.name.endswith("_voiceover.mp4") or "_take" in cand.name:
                continue
            return cand
    name = Path(video_name or sidecar.get("video") or "video").name
    if name.lower() == "sample.mp4":
        demo = STATIC / "demo" / "sample.mp4"
        if demo.is_file():
            return demo
    raise FileNotFoundError(
        "Source video is not on disk. Open the video again so it can be saved, then export."
    )


def _run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "ffmpeg failed").strip()
        raise RuntimeError(err[-4000:])


def _valid_takes(video_name: str, sidecar: dict) -> list[dict]:
    folder = video_dir(video_name, mkdir=False)
    takes = []
    for take in sidecar.get("takes") or []:
        name = Path(take.get("audio") or "").name
        audio = resolve_media(video_name, name)
        if audio is None and folder.is_dir():
            audio = folder / name
        start = float(take.get("start") or 0)
        end = float(take.get("end") or 0)
        if not audio or not audio.is_file() or end < start:
            continue
        takes.append({**take, "_audio": audio, "_start": start, "_end": end})
    if not takes:
        raise RuntimeError("No complete takes to export yet. Press R to record, then R to stop.")
    return takes


def _segments_for(take: dict) -> list[dict]:
    segs = [s for s in (take.get("segments") or []) if float(s.get("duration") or 0) >= 0.02]
    if segs:
        return segs
    start, end = take["_start"], take["_end"]
    span = max(0.04, end - start)
    audio_len = media_duration(take["_audio"]) or float(take.get("audio_elapsed") or span)
    extra = max(0.0, audio_len - span)
    out = [{"kind": "play", "video_start": start, "video_end": end, "duration": span}]
    if extra >= 0.05:
        out.append({"kind": "hold", "video_at": end, "duration": extra})
    return out


def _filter_script(segments: list[dict], fps: float, video_duration: float) -> str:
    n = len(segments)
    if n == 1:
        splits = "[0:v]split=1[s0]"
    else:
        labels = "".join(f"[s{i}]" for i in range(n))
        splits = f"[0:v]split={n}{labels}"
    parts = [splits]
    outs = []
    frame = 1.0 / max(fps, 1.0)
    last = max(0.0, video_duration - frame * 2)
    for i, seg in enumerate(segments):
        kind = seg.get("kind")
        if kind == "play":
            a = min(max(0.0, float(seg.get("video_start") or 0)), last)
            b = min(max(a + frame, float(seg.get("video_end") or 0)), video_duration)
            parts.append(f"[s{i}]trim=start={a:.4f}:end={b:.4f},setpts=PTS-STARTPTS[v{i}]")
        else:
            t = min(max(0.0, float(seg.get("video_at") or 0)), last)
            hold = max(frame, float(seg.get("duration") or 0))
            pad = max(0.0, hold - frame)
            parts.append(
                f"[s{i}]trim=start={t:.4f}:end={t + frame:.4f},setpts=PTS-STARTPTS,"
                f"tpad=stop_mode=clone:stop_duration={pad:.4f}[v{i}]"
            )
        outs.append(f"[v{i}]")
    parts.append(f"{''.join(outs)}concat=n={n}:v=1:a=0[vout]")
    return ";\n".join(parts)


def _export_take(source: Path, take: dict, dest: Path, fps: float, video_duration: float) -> None:
    segments = _segments_for(take)
    graph = _filter_script(segments, fps, video_duration).replace("\n", "")
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-i",
            str(take["_audio"]),
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
            "-map",
            "1:a",
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
            str(dest),
        ]
    )


def export_voiceover(video_name: str, sidecar: dict) -> Path:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg/ffprobe is not on PATH.")
    source = find_source(video_name, sidecar)
    takes = _valid_takes(video_name, sidecar)
    fps = stream_fps(source)
    video_duration = media_duration(source) or float(sidecar.get("resume_at") or 0) or 1.0
    folder = video_dir(video_name)
    stem = safe_stem(video_name or sidecar.get("video") or "video")
    final = folder / f"{stem}_voiceover.mp4"

    with tempfile.TemporaryDirectory(dir=str(folder), prefix="export_") as tmp:
        tmp_path = Path(tmp)
        parts = []
        for i, take in enumerate(takes, start=1):
            clip = tmp_path / f"take{i:02d}.mp4"
            _export_take(source, take, clip, fps, video_duration)
            parts.append(clip)
        if len(parts) == 1:
            shutil.copy2(parts[0], final)
        else:
            listing = tmp_path / "concat.txt"
            listing.write_text(
                "\n".join(f"file '{p.name}'" for p in parts) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    "concat.txt",
                    "-c",
                    "copy",
                    str(final),
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(tmp_path),
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "ffmpeg concat failed").strip()
                raise RuntimeError(err[-4000:])
    return final

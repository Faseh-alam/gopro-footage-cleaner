"""Baked 5× overspeed skim proxies for stutter-free high-rate review.

Originals stay untouched for trim/export/share. Skim files are short progressive
MP4s (duration ≈ T/5) played near 1.0× so UI rates 2×–5× stay smooth. Annotation
times always stay in original timeline seconds on the client.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .encode_pool import encode_slots

_lock = threading.Lock()
_jobs: dict[str, dict] = {}

# Bump when encoder settings change so old caches are ignored.
_SKIM_VERSION = "v1-skim5x-720"
_SKIM_FACTOR = 5
_SKIM_NAME = "skim_5x.mp4"


def _skims_disabled() -> bool:
    return os.environ.get("GOPRO_SKIM_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def _cache_dir() -> Path:
    path = Path.home() / ".cache" / "gopro-cleaner" / "skims"
    path.mkdir(parents=True, exist_ok=True)
    return path


def skim_cache_root() -> Path:
    """Public accessor for HTTP routes serving skim assets."""
    return _cache_dir()


def skim_factor() -> int:
    return _SKIM_FACTOR


def _cache_key(source: Path) -> str:
    stat = source.stat()
    digest = hashlib.sha256(
        f"{_SKIM_VERSION}:{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    )
    return digest.hexdigest()[:20]


def _skim_dir(source: Path) -> Path:
    return _cache_dir() / _cache_key(source)


def _skim_path(source: Path) -> Path:
    return _skim_dir(source) / _SKIM_NAME


def _skim_url(source: Path) -> str:
    return f"/api/eager/skim/{_cache_key(source)}/{_SKIM_NAME}"


def _probe_duration_seconds(source: Path, timeout: float = 4.0) -> float:
    """Best-effort duration for progress % — keep it short so it never stalls work."""
    try:
        from .ffmpeg_tools import ffprobe_bin

        result = subprocess.run(
            [
                ffprobe_bin(),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
        return max(0.0, float((result.stdout or "").strip() or 0))
    except Exception:
        return 0.0


def _software_threads() -> int:
    cores = os.cpu_count() or 4
    return max(2, min(6, cores // 2))


def _software_encoder_args() -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "fastdecode",
        "-crf",
        "34",
        "-threads",
        str(_software_threads()),
    ]


_WINDOWS_HW_CANDIDATES: list[tuple[str, list[str]]] = [
    (
        "nvenc",
        [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p1",
            "-tune",
            "ll",
            "-b:v",
            "1500k",
            "-maxrate",
            "2200k",
            "-bufsize",
            "3300k",
        ],
    ),
    ("qsv", ["-c:v", "h264_qsv", "-preset", "veryfast", "-b:v", "1500k"]),
    ("amf", ["-c:v", "h264_amf", "-quality", "speed", "-b:v", "1500k"]),
]

_win_encoder_args: list[str] | None = None


def _encoder_works(encoder_args: list[str]) -> bool:
    from .ffmpeg_tools import ffmpeg_bin

    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=black:s=256x256:d=0.2:r=12",
        *encoder_args,
        "-frames:v",
        "3",
        "-f",
        "null",
        "-",
    ]
    try:
        return subprocess.run(command, capture_output=True, timeout=20, check=False).returncode == 0
    except Exception:
        return False


def _skim_encoder_args() -> list[str]:
    """Prefer hardware encode when available; otherwise fast software x264."""
    # Reuse the same preference as careful preview when possible.
    try:
        from . import preview_proxy as preview

        return list(preview._preview_encoder_args())
    except Exception:
        pass

    system = platform.system()
    if system == "Darwin":
        return [
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            "1500k",
            "-maxrate",
            "2200k",
            "-bufsize",
            "3300k",
        ]
    if system == "Windows":
        global _win_encoder_args
        prefer = (os.environ.get("GOPRO_PREVIEW_ENCODER") or "auto").strip().lower()
        if prefer in {"x264", "libx264", "software", "cpu"}:
            return _software_encoder_args()
        for name, args in _WINDOWS_HW_CANDIDATES:
            if prefer in {name, f"h264_{name}"}:
                return list(args)
        if _win_encoder_args is None:
            _win_encoder_args = next(
                (args for _, args in _WINDOWS_HW_CANDIDATES if _encoder_works(args)),
                _software_encoder_args(),
            )
        return list(_win_encoder_args)
    return _software_encoder_args()


def _hwaccel_input_args() -> list[str]:
    if platform.system() == "Darwin":
        return ["-hwaccel", "videotoolbox"]
    if platform.system() == "Windows":
        return ["-hwaccel", "auto"]
    return []


def _vf_filter() -> str:
    # Speed timeline by 5× then emit 30fps — output duration ≈ T/5 regardless of
    # source fps. Scale after the drop so we only resize kept frames.
    return (
        f"setpts=PTS/{_SKIM_FACTOR},"
        "fps=30,"
        "scale='min(720,iw)':-2:flags=fast_bilinear"
    )


def _clear_dir_contents(directory: Path) -> None:
    try:
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _build_skim(source: Path, dest: Path, job_key: str, process_holder: list) -> None:
    from .ffmpeg_tools import ffmpeg_bin

    duration = 0.0
    probe_holder: list[float] = [0.0]

    def _probe_async() -> None:
        for _ in range(4):
            value = _probe_duration_seconds(source)
            if value > 0:
                probe_holder[0] = value
                return
            time.sleep(2.0)

    threading.Thread(target=_probe_async, daemon=True, name="skim-probe").start()

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial.mp4")
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass

    command = [
        ffmpeg_bin(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-fflags",
        "+genpts+fastseek",
        "-probesize",
        "32k",
        "-analyzeduration",
        "0",
        *_hwaccel_input_args(),
        "-i",
        str(source),
        "-an",
        "-vf",
        _vf_filter(),
        *_skim_encoder_args(),
        "-pix_fmt",
        "yuv420p",
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(tmp),
    ]

    popen_kwargs: dict = {}
    if platform.system() == "Windows":
        popen_kwargs["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
    else:
        popen_kwargs["preexec_fn"] = lambda: os.nice(10)  # noqa: PLW1509

    stderr_log = dest.parent / "stderr.log"
    stderr_handle = open(stderr_log, "w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=stderr_handle,
        text=True,
        bufsize=1,
        **popen_kwargs,
    )
    process_holder.append(process)
    with _lock:
        if job_key in _jobs:
            _jobs[job_key]["process"] = process

    assert process.stdout is not None
    for line in process.stdout:
        with _lock:
            job = _jobs.get(job_key)
            if not job or job.get("status") != "running":
                process.terminate()
                break
        # ffmpeg out_time_ms is actually microseconds (same as out_time_us).
        if line.startswith(("out_time_us=", "out_time_ms=")):
            duration = probe_holder[0] or duration
            if duration <= 0:
                continue
            try:
                out_us = int(line.split("=", 1)[1].strip() or 0)
                # Progress clock is on the shortened output timeline (≈ T/5).
                out_sec = out_us / 1_000_000.0
                target = duration / float(_SKIM_FACTOR)
                pct = min(99, int((out_sec / target) * 100)) if target > 0 else 0
            except ValueError:
                continue
            with _lock:
                if job_key in _jobs and _jobs[job_key].get("status") == "running":
                    _jobs[job_key]["progress"] = max(int(_jobs[job_key].get("progress") or 0), pct)

    code = process.wait()
    try:
        stderr_handle.close()
    except OSError:
        pass
    err = ""
    try:
        err = stderr_log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        err = ""
    finally:
        try:
            stderr_log.unlink()
        except OSError:
            pass

    if code != 0:
        joined = " ".join(command)
        if any(hw in joined for hw in ("nvenc", "qsv", "amf")):
            soft = [
                ffmpeg_bin(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-an",
                "-vf",
                _vf_filter(),
                *_software_encoder_args(),
                "-pix_fmt",
                "yuv420p",
                "-g",
                "30",
                "-keyint_min",
                "30",
                "-sc_threshold",
                "0",
                "-movflags",
                "+faststart",
                str(tmp),
            ]
            retry = subprocess.run(soft, capture_output=True, text=True, check=False)
            if retry.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                tmp.replace(dest)
                return
            err = (retry.stderr or err or "").strip()
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise RuntimeError(err.strip() or "ffmpeg failed while building 5× skim")

    if not tmp.is_file() or tmp.stat().st_size <= 0:
        raise RuntimeError("ffmpeg finished but skim file is missing or empty")
    tmp.replace(dest)


def cancel_skim(source: Path) -> None:
    source = source.expanduser().resolve()
    key = str(source)
    with _lock:
        job = _jobs.get(key)
        if not job:
            return
        proc = job.get("process")
        job["status"] = "cancelled"
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        _jobs.pop(key, None)
    try:
        dest = _skim_path(source)
        if not dest.is_file():
            shutil.rmtree(dest.parent, ignore_errors=True)
    except OSError:
        pass


def _public_job(job: dict, extra: dict | None = None) -> dict:
    out = {
        key: job.get(key)
        for key in (
            "status",
            "progress",
            "path",
            "url",
            "ready",
            "cached",
            "error",
            "message",
            "source_bytes",
            "factor",
        )
        if job.get(key) is not None
    }
    if extra:
        out.update(extra)
    return out


def skim_status(source: Path, *, start: bool = False) -> dict:
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    key = str(source)
    size = source.stat().st_size
    dest = _skim_path(source)
    url = _skim_url(source)

    if dest.is_file() and dest.stat().st_size > 0:
        return {
            "status": "ready",
            "path": str(dest),
            "url": url,
            "ready": True,
            "cached": True,
            "progress": 100,
            "factor": _SKIM_FACTOR,
            "source_bytes": size,
            "message": "5× skim ready",
        }

    if _skims_disabled():
        return {
            "status": "skipped",
            "reason": "disabled",
            "message": "Skim proxies disabled (GOPRO_SKIM_DISABLED)",
            "factor": _SKIM_FACTOR,
            "source_bytes": size,
            "ready": False,
        }

    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") == "running":
            return _public_job(
                job,
                {
                    "source_bytes": size,
                    "url": url,
                    "factor": _SKIM_FACTOR,
                    "ready": False,
                },
            )
        if job and job.get("status") in {"error", "cancelled"}:
            _jobs.pop(key, None)

    if not start:
        return {
            "status": "missing",
            "progress": 0,
            "factor": _SKIM_FACTOR,
            "source_bytes": size,
            "ready": False,
            "message": "5× skim not built yet",
        }

    # Prefer skim over careful HLS — cancel a competing preview for this source
    # so the shared encode slot frees for the stutter-critical path.
    try:
        from .preview_proxy import cancel_preview

        cancel_preview(source)
    except Exception:
        pass

    with _lock:
        _jobs[key] = {
            "status": "running",
            "progress": 0,
            "process": None,
            "source_bytes": size,
            "factor": _SKIM_FACTOR,
            "url": url,
            "ready": False,
            "message": "Building 5× skim…",
        }

    def worker() -> None:
        process_holder: list = []
        acquired = encode_slots.acquire(blocking=False)
        if not acquired:
            with _lock:
                if _jobs.get(key, {}).get("status") == "running":
                    _jobs[key]["status"] = "queued"
                    _jobs[key]["message"] = "Queued — waiting for the current encode to finish…"
            acquired = encode_slots.acquire(timeout=3600)
        if not acquired:
            with _lock:
                if _jobs.get(key, {}).get("status") in {"running", "queued"}:
                    _jobs[key] = {
                        "status": "error",
                        "error": "Skim encode timed out waiting for a free slot",
                        "progress": 0,
                        "process": None,
                        "source_bytes": size,
                        "factor": _SKIM_FACTOR,
                        "ready": False,
                    }
            return
        try:
            with _lock:
                job = _jobs.get(key)
                if not job or job.get("status") not in {"running", "queued"}:
                    return
                _jobs[key]["status"] = "running"
                _jobs[key]["message"] = "Encoding 5× skim…"
            _clear_dir_contents(dest.parent)
            _build_skim(source, dest, key, process_holder)
            with _lock:
                if _jobs.get(key, {}).get("status") != "running":
                    shutil.rmtree(dest.parent, ignore_errors=True)
                    return
            if not dest.is_file() or dest.stat().st_size <= 0:
                raise RuntimeError("ffmpeg finished but skim file is missing")
            with _lock:
                _jobs[key] = {
                    "status": "ready",
                    "path": str(dest),
                    "url": url,
                    "ready": True,
                    "cached": False,
                    "progress": 100,
                    "process": None,
                    "source_bytes": size,
                    "factor": _SKIM_FACTOR,
                    "message": "5× skim ready",
                }
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(dest.parent, ignore_errors=True)
            with _lock:
                if _jobs.get(key, {}).get("status") == "running":
                    _jobs[key] = {
                        "status": "error",
                        "error": str(exc),
                        "progress": 0,
                        "process": None,
                        "source_bytes": size,
                        "factor": _SKIM_FACTOR,
                        "ready": False,
                    }
        finally:
            encode_slots.release()

    thread = threading.Thread(target=worker, daemon=True, name=f"skim-{source.name}")
    thread.start()
    with _lock:
        if key in _jobs:
            _jobs[key]["thread"] = thread
    return {
        "status": "running",
        "progress": 0,
        "factor": _SKIM_FACTOR,
        "source_bytes": size,
        "url": url,
        "ready": False,
        "message": "Building 5× skim…",
    }


def ensure_skim_5x(source: Path) -> dict:
    """Enqueue a 5× skim encode (single-flight)."""
    return skim_status(source, start=True)


def resolve_skim(source: Path) -> Path:
    status = skim_status(source, start=False)
    if status.get("status") == "ready":
        return Path(status["path"])
    raise RuntimeError(status.get("error") or "Skim not ready")

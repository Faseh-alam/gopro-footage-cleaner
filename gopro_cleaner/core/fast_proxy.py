"""Zero-encode fast review sources — instant open, smooth 5× skim.

Nothing here transcodes. The review player picks the cheapest source that keeps
the ORIGINAL timeline 1:1 (so annotations, resume and share marks never shift):

1. ``lrv``       GoPro's own low-res proxy (``GL010042.LRV`` next to
                 ``GX010042.MP4``). ~540p / few Mbps, so 5× playback decodes
                 easily on 8GB / i5-6xxx review machines. Available instantly —
                 no wait at all.
2. ``ssd_copy``  No LRV: copy the original to a local SSD cache in the
                 background. The card original streams meanwhile; the player
                 hot-swaps when the copy lands.
3. ``original``  Already on a local fixed disk (or copy not worth it) — stream
                 it directly.

Trim / share / export keep using the untouched original path.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import string
import sys
import threading
from pathlib import Path
from urllib.parse import quote

_lock = threading.Lock()
_jobs: dict[str, dict] = {}

# Bump if the cache layout changes.
_FAST_VERSION = "v1-fast-lrv-ssd"

_PROXY_NAME = "proxy.mp4"
_ORIGINAL_NAME = "original.mp4"

# Copies are I/O bound, not CPU bound — a couple in flight keeps prefetch warm
# without thrashing the source drive.
_copy_slots = threading.Semaphore(2)

# Don't SSD-cache absurdly large files by default (GB).
_MAX_COPY_GB = float(os.environ.get("GOPRO_FAST_MAX_COPY_GB", "24") or 24)
# Total cache budget — oldest review copies are pruned past this (GB).
_CACHE_BUDGET_GB = float(os.environ.get("GOPRO_FAST_CACHE_GB", "60") or 60)


def _fast_disabled() -> bool:
    return os.environ.get("GOPRO_FAST_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def _copy_disabled() -> bool:
    """Set GOPRO_FAST_NO_COPY=1 to use LRV only and never copy originals."""
    return os.environ.get("GOPRO_FAST_NO_COPY", "").strip().lower() in {"1", "true", "yes"}


def _cache_dir() -> Path:
    override = (os.environ.get("GOPRO_FAST_CACHE") or "").strip()
    path = Path(override) if override else Path.home() / ".cache" / "gopro-cleaner" / "fast"
    path.mkdir(parents=True, exist_ok=True)
    return path


def fast_cache_root() -> Path:
    """Public accessor for the HTTP route serving cached review media."""
    return _cache_dir()


def _cache_key(source: Path) -> str:
    stat = source.stat()
    digest = hashlib.sha256(
        f"{_FAST_VERSION}:{source.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()
    )
    return digest.hexdigest()[:20]


def _stream_url(path: Path) -> str:
    return f"/api/eager/stream?path={quote(str(path), safe='')}"


def _cache_url(key: str, name: str) -> str:
    return f"/api/eager/fast/{key}/{name}"


# ---------------------------------------------------------------------------
# LRV discovery
# ---------------------------------------------------------------------------
# Camera prefixes that carry real video. The proxy always uses GL for the same
# file number: GX010060.MP4 (AVC) / GH010060.MP4 (HEVC) → GL010060.LRV.
_VIDEO_PREFIXES = ("GX", "GH", "GP", "GS")
_PROXY_PREFIX = "GL"


def find_lrv(source: Path) -> Path | None:
    """GoPro low-res proxy for this MP4, if the camera wrote one.

    HERO5+ pairs ``GX``/``GH`` video with a ``GL`` proxy of the same file number
    (``GH010060.MP4`` → ``GL010060.LRV``, either extension case); older cameras
    reuse the same stem. Offloaded footage may also carry a ``__C1234`` card
    suffix, so match on the file-number core and refuse to guess when several
    MP4s in the folder share that number.
    """
    try:
        folder = source.parent
        if not folder.is_dir():
            return None
    except OSError:
        return None

    stem = source.stem
    core, card_suffix = _name_parts(stem)

    # Direct hits: same stem (older cameras), then the GL proxy for this number.
    # Cards hold thousands of files, so probe names before listing the folder.
    probes = [stem + ".LRV", stem + ".lrv"]
    if core:
        tail = f"{core}__{card_suffix}" if card_suffix else core
        probes += [f"{_PROXY_PREFIX}{tail}.LRV", f"{_PROXY_PREFIX}{tail}.lrv"]
    for name in probes:
        candidate = folder / name
        if _usable_file(candidate):
            # Case-insensitive filesystems match either extension case, so
            # report the real name rather than whichever spelling we probed.
            try:
                return candidate.resolve()
            except OSError:
                return candidate
    if not core:
        return None

    # A bare GL proxy next to a card-suffixed MP4 (or vice versa) needs the
    # ambiguity check below, as does anything with an unexpected prefix.
    return _scan_for_lrv(folder, core, card_suffix)


def _scan_for_lrv(folder: Path, core: str, card_suffix: str) -> Path | None:
    """Single directory pass: LRVs for this file number + how many MP4s claim it."""
    tagged: list[Path] = []
    bare: list[Path] = []
    mp4_peers = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                name = entry.name
                dot = name.rfind(".")
                if dot < 0:
                    continue
                suffix = name[dot:].lower()
                if suffix not in {".lrv", ".mp4"}:
                    continue
                other_core, other_card = _name_parts(name[:dot])
                if other_core != core:
                    continue
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                if suffix == ".mp4":
                    mp4_peers += 1
                    continue
                path = Path(entry.path)
                if not _usable_file(path):
                    continue
                if other_card:
                    if other_card == card_suffix:
                        tagged.append(path)
                else:
                    bare.append(path)
    except OSError:
        return None

    if len(tagged) == 1:
        return tagged[0]
    # GoPro numbering repeats across cards, so a bare proxy is only safe when a
    # single MP4 in this folder claims the number.
    if len(bare) == 1 and mp4_peers <= 1:
        return bare[0]
    return None


def _usable_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _name_parts(stem: str) -> tuple[str, str]:
    """(file-number core, card suffix) for ``GX010042__C1234`` style stems.

    The two-letter camera prefix is dropped so an HEVC ``GH010060`` lines up
    with its ``GL010060`` proxy. Case-folded for cross-platform matching.
    """
    base, _, card_suffix = stem.partition("__")
    upper = base.upper()
    if len(base) >= 3 and (
        upper.startswith(_VIDEO_PREFIXES) or upper.startswith(_PROXY_PREFIX)
    ):
        return base[2:].lower(), card_suffix
    return base.lower(), card_suffix


# ---------------------------------------------------------------------------
# Drive classification
# ---------------------------------------------------------------------------
def _is_local_fixed(path: Path) -> bool:
    """True when the file already lives on an internal disk (no copy needed)."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False

    if sys.platform == "win32":
        try:
            import ctypes

            drive = str(resolved.drive or "").rstrip("\\/")
            if not drive or drive[0].upper() not in string.ascii_uppercase:
                return False
            # 3 = DRIVE_FIXED. Removable card readers report 2.
            return ctypes.windll.kernel32.GetDriveTypeW(f"{drive}\\") == 3
        except Exception:
            return False

    text = str(resolved)
    return not any(text.startswith(prefix) for prefix in ("/Volumes/", "/media/", "/mnt/", "/run/media/"))


def _copy_worth_it(source: Path) -> bool:
    if _copy_disabled():
        return False
    try:
        size = source.stat().st_size
    except OSError:
        return False
    if size <= 0 or size > _MAX_COPY_GB * (1024**3):
        return False
    try:
        usage = shutil.disk_usage(_cache_dir())
        # Leave headroom so the review machine never fills its system drive.
        return usage.free > size * 1.25
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Background copy
# ---------------------------------------------------------------------------
def _prune_cache(keep_key: str) -> None:
    """Drop the least-recently-used cache entries once past the budget."""
    root = _cache_dir()
    budget = _CACHE_BUDGET_GB * (1024**3)
    entries: list[tuple[float, int, Path]] = []
    total = 0
    try:
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            size = sum(f.stat().st_size for f in entry.glob("*") if f.is_file())
            total += size
            entries.append((entry.stat().st_mtime, size, entry))
    except OSError:
        return
    if total <= budget:
        return
    for _mtime, size, entry in sorted(entries):
        if total <= budget:
            break
        if entry.name == keep_key:
            continue
        try:
            shutil.rmtree(entry, ignore_errors=True)
            total -= size
        except OSError:
            continue


def _copy_with_progress(src: Path, dest: Path, job_key: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".partial")
    try:
        if tmp.exists():
            tmp.unlink()
    except OSError:
        pass

    total = max(1, src.stat().st_size)
    copied = 0
    chunk = 4 * 1024 * 1024
    with open(src, "rb") as reader, open(tmp, "wb") as writer:
        while True:
            with _lock:
                job = _jobs.get(job_key)
                if not job or job.get("status") != "copying":
                    raise RuntimeError("cancelled")
            block = reader.read(chunk)
            if not block:
                break
            writer.write(block)
            copied += len(block)
            pct = min(99, int(copied / total * 100))
            with _lock:
                job = _jobs.get(job_key)
                if job is not None:
                    job["progress"] = max(int(job.get("progress") or 0), pct)
    tmp.replace(dest)


def _start_copy(source: Path, src_file: Path, dest: Path, kind: str) -> None:
    key = str(source.resolve())
    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") == "copying":
            return
        _jobs[key] = {
            "status": "copying",
            "progress": 0,
            "kind": kind,
            "message": "Copying review copy to SSD…",
        }

    def worker() -> None:
        acquired = _copy_slots.acquire(timeout=1800)
        if not acquired:
            with _lock:
                _jobs.pop(key, None)
            return
        try:
            _copy_with_progress(src_file, dest, key)
            with _lock:
                _jobs[key] = {"status": "ready", "progress": 100, "kind": kind}
            _prune_cache(dest.parent.name)
        except Exception as exc:  # noqa: BLE001
            try:
                partial = dest.with_suffix(".partial")
                if partial.exists():
                    partial.unlink()
            except OSError:
                pass
            with _lock:
                if str(exc) == "cancelled":
                    _jobs.pop(key, None)
                else:
                    _jobs[key] = {"status": "error", "error": str(exc), "kind": kind, "progress": 0}
        finally:
            _copy_slots.release()

    threading.Thread(target=worker, daemon=True, name=f"fast-copy-{source.name}").start()


def cancel_fast(source: Path) -> None:
    """Stop an in-flight copy for this source (leaves finished caches alone)."""
    try:
        key = str(source.expanduser().resolve())
    except OSError:
        key = str(source)
    with _lock:
        job = _jobs.get(key)
        if job and job.get("status") == "copying":
            job["status"] = "cancelled"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def fast_status(source: Path, *, start: bool = False) -> dict:
    """Best available zero-encode source for review playback.

    ``url`` is always safe to play right now. ``ready`` means it is the final
    (fastest) source, so the client can stop polling.
    """
    source = source.expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(source)

    size = source.stat().st_size
    original_url = _stream_url(source)
    if _fast_disabled():
        return {
            "status": "ready",
            "kind": "original",
            "url": original_url,
            "ready": True,
            "progress": 100,
            "timeline": "1:1",
            "source_bytes": size,
            "message": "Fast sources disabled (GOPRO_FAST_DISABLED)",
        }

    key = _cache_key(source)
    cache_dir = _cache_dir() / key
    lrv = find_lrv(source)

    # --- LRV: playable instantly, even straight off the card. ---------------
    if lrv is not None:
        cached = cache_dir / _PROXY_NAME
        if cached.is_file() and cached.stat().st_size > 0:
            return {
                "status": "ready",
                "kind": "lrv",
                "url": _cache_url(key, _PROXY_NAME),
                "ready": True,
                "cached": True,
                "progress": 100,
                "timeline": "1:1",
                "source_bytes": size,
                "proxy_bytes": cached.stat().st_size,
                "message": "GoPro LRV proxy (SSD) — 5× ready",
            }
        if start and not _is_local_fixed(lrv) and _copy_worth_it(lrv):
            _start_copy(source, lrv, cached, "lrv")
        # Serve the LRV in place right now — it is tiny, so even a card keeps up.
        with _lock:
            job = _jobs.get(str(source))
        return {
            "status": "ready",
            "kind": "lrv",
            "url": _stream_url(lrv),
            "ready": True,
            "cached": False,
            "progress": int((job or {}).get("progress") or 0) if job else 0,
            "timeline": "1:1",
            "source_bytes": size,
            "lrv_path": str(lrv),
            "message": "GoPro LRV proxy — 5× ready",
        }

    # --- No LRV: SSD copy of the original, streaming the card meanwhile. ----
    cached_original = cache_dir / _ORIGINAL_NAME
    if cached_original.is_file() and cached_original.stat().st_size > 0:
        return {
            "status": "ready",
            "kind": "ssd_copy",
            "url": _cache_url(key, _ORIGINAL_NAME),
            "ready": True,
            "cached": True,
            "progress": 100,
            "timeline": "1:1",
            "source_bytes": size,
            "message": "Original on SSD — no card contention",
        }

    if _is_local_fixed(source):
        return {
            "status": "ready",
            "kind": "original",
            "url": original_url,
            "ready": True,
            "progress": 100,
            "timeline": "1:1",
            "source_bytes": size,
            "message": "Original already on local disk",
        }

    with _lock:
        job = _jobs.get(str(source))
    if job and job.get("status") == "copying":
        return {
            "status": "copying",
            "kind": "ssd_copy",
            "url": original_url,
            "ready": False,
            "progress": int(job.get("progress") or 0),
            "timeline": "1:1",
            "source_bytes": size,
            "message": f"Copying to SSD {int(job.get('progress') or 0)}% — playing from card meanwhile",
        }

    if start and _copy_worth_it(source):
        _start_copy(source, source, cached_original, "ssd_copy")
        return {
            "status": "copying",
            "kind": "ssd_copy",
            "url": original_url,
            "ready": False,
            "progress": 0,
            "timeline": "1:1",
            "source_bytes": size,
            "message": "Copying to SSD — playing from card meanwhile",
        }

    return {
        "status": "ready",
        "kind": "original",
        "url": original_url,
        "ready": True,
        "progress": 100,
        "timeline": "1:1",
        "source_bytes": size,
        "message": "Streaming original (no LRV proxy on this footage)",
    }


def ensure_fast(source: Path) -> dict:
    """Warm the fast source for a file we expect to review shortly."""
    return fast_status(source, start=True)

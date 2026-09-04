"""AWS S3 sync via s5cmd (preferred) or AWS CLI.

Uploads run in an **external Command Prompt / Terminal** so a server restart
does **not** stop them. Output is tee'd to a log file under ``state/aws_logs/``.
The offloader watches those logs and shows size / speed / ETA in the UI.
On startup it re-attaches to any still-running uploads (open CMD + log).

Prefers ``s5cmd sync`` first (default workers — usually faster), then retries with
``s5cmd --numworkers N`` if that fails. Falls back to ``aws s3 sync``. Failed
syncs auto-retry in the CMD script; the UI also has Restart + size-verify
(local vs S3) before optional local delete.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import BATCHES_SUBDIR, STATE_DIR, ensure_dirs, load_config

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_on_batch_deleted = None
_monitor_started = False
JOBS_FILE = STATE_DIR / "aws_jobs.json"
LOG_DIR = STATE_DIR / "aws_logs"
EXIT_MARKER = "OFFLOADER_EXIT:"
VERIFY_MARKER = "OFFLOADER_VERIFY:"

_SPEED_RE = re.compile(r"([\d.]+)\s*(MiB|MB|GiB|GB)/s", re.IGNORECASE)
_COMPLETED_RE = re.compile(
    r"Completed\s+([\d.]+)\s*(MiB|MB|GiB|GB|KiB|KB|B)(?:\s*/\s*([\d.]+)\s*(MiB|MB|GiB|GB|KiB|KB|B))?",
    re.IGNORECASE,
)
_FILES_REMAINING_RE = re.compile(r"with\s+(\d+)\s+file\(s\)\s+remaining", re.IGNORECASE)
_UPLOAD_RE = re.compile(
    r"^(?:upload|copy|download):\s+(.+?)\s+to\s+s3://",
    re.IGNORECASE,
)
# s5cmd: cp local s3://...
_S5CMD_CP_RE = re.compile(
    r"^(?:cp|mv)\s+(.+?)\s+s3://",
    re.IGNORECASE,
)
# Batch folder names may contain spaces (e.g. "batch 1") — do not stop at \s.
_BATCH_IN_PATH_RE = re.compile(
    r"[\\/]Batches[\\/]([^\\\"'/]+?)(?=[\\/]|$)",
    re.IGNORECASE,
)
# Match double-quoted, single-quoted, or bare args (spaces only work when quoted).
_SYNC_ARGS_RE = re.compile(
    r"(?:s3\s+sync|sync)\s+"
    r"(?:\"([^\"]+)\"|'([^']+)'|(\S+))\s+"
    r"(?:\"(s3://[^\"]+)\"|'(s3://[^']+)'|(s3://\S+))",
    re.IGNORECASE,
)
_TOTAL_SIZE_RE = re.compile(r"Total Size:\s*(\d+)", re.IGNORECASE)
_TOTAL_OBJECTS_RE = re.compile(r"Total Objects:\s*(\d+)", re.IGNORECASE)
_S5CMD_DU_RE = re.compile(
    r"([\d.]+)\s*(?:bytes|[KMGT]i?B)\s+in\s+(\d+)\s+objects?",
    re.IGNORECASE,
)
# Verification listing only — never applied to s5cmd/aws sync (the upload itself).
AWS_VERIFY_TIMEOUT_SECONDS = 30 * 60
# 0.01% of the local byte count, with a 1 MiB floor (small batches stay strict;
# a ~3.2 TB batch is allowed ~320 MB of listing slack).
SIZE_TOLERANCE_PERCENT = 0.0001
SIZE_TOLERANCE_FLOOR_BYTES = 1024 * 1024
_SIZE_TOLERANCE_BYTES = SIZE_TOLERANCE_FLOOR_BYTES  # back-compat alias


def aws_cli_available() -> bool:
    return shutil.which("aws") is not None


def s5cmd_available() -> bool:
    return shutil.which("s5cmd") is not None


def upload_tool_available() -> bool:
    return s5cmd_available() or aws_cli_available()


def preferred_uploader() -> str:
    """Return 's5cmd' or 'aws' — s5cmd preferred when both exist."""
    if s5cmd_available():
        return "s5cmd"
    if aws_cli_available():
        return "aws"
    return ""


def _numworkers() -> int:
    try:
        n = int(load_config().get("s5cmd_numworkers") or 20)
    except (TypeError, ValueError):
        n = 20
    return max(1, min(n, 256))


def _upload_retries() -> int:
    try:
        n = int(load_config().get("aws_upload_retries") or 5)
    except (TypeError, ValueError):
        n = 5
    return max(1, min(n, 20))


def test_aws_connection(s3_uri: str) -> dict:
    """Upload a tiny empty file via AWS CLI credentials (`aws configure`)."""
    if not upload_tool_available():
        raise RuntimeError(
            "Neither s5cmd nor AWS CLI found. Install s5cmd (preferred) or AWS CLI v2, then run `aws configure`."
        )

    base = normalize_s3_uri(s3_uri)
    key = f"{base}_offloader_connection_test.txt"
    tool = preferred_uploader()

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "offloader_connection_test.txt"
        local.write_text("", encoding="utf-8")
        if tool == "s5cmd":
            put = subprocess.run(
                ["s5cmd", "cp", str(local), key],
                capture_output=True,
                text=True,
            )
        else:
            put = subprocess.run(
                ["aws", "s3", "cp", str(local), key],
                capture_output=True,
                text=True,
            )
        if put.returncode != 0:
            detail = (put.stderr or put.stdout or f"{tool} upload failed").strip()
            raise RuntimeError(detail)

        if tool == "s5cmd":
            delete = subprocess.run(
                ["s5cmd", "rm", key],
                capture_output=True,
                text=True,
            )
        else:
            delete = subprocess.run(
                ["aws", "s3", "rm", key],
                capture_output=True,
                text=True,
            )
        cleaned = delete.returncode == 0

    return {
        "ok": True,
        "uploader": tool,
        "message": (
            f"AWS OK via {tool} — uploaded and verified write to {key}"
            + (" (test file removed)" if cleaned else " (could not delete test file; upload still worked)")
        ),
        "s3_key": key,
        "cleaned": cleaned,
    }


def normalize_s3_uri(uri: str) -> str:
    value = uri.strip().rstrip("/") + "/"
    if not value.startswith("s3://"):
        raise ValueError("S3 URI must start with s3://")
    return value


def batch_s3_prefix(s3_uri: str, batch_name: str) -> str:
    """Build ``s3://bucket/footage/<batch>/`` for a flat batch upload.

    Local layout is ``Batches/<batch>/*.MP4`` (no card subfolder), so S3 is the
    same flat prefix. If ``s3_uri`` already ends with the batch folder name,
    do not nest it again (avoids ``…/batch 1/batch 1/``).
    """
    base = normalize_s3_uri(s3_uri)
    name = batch_name.strip().strip("/")
    if not name:
        raise ValueError("Batch name is required")
    last = base.rstrip("/").rsplit("/", 1)[-1]
    if last.lower() == name.lower():
        return base.rstrip("/") + "/"
    return f"{base}{name}/"


def _sync_local_arg(path: Path) -> str:
    """Local folder for aws/s5cmd sync — trailing slash syncs folder *contents*."""
    text = str(path)
    # Forward slashes are accepted by aws CLI and s5cmd on Windows.
    text = text.replace("\\", "/")
    if not text.endswith("/"):
        text += "/"
    return text


def list_local_batch_roots(ssd1: str, ssd2: str, batch_name: str) -> list[Path]:
    roots = []
    for ssd in (ssd1, ssd2):
        if not ssd:
            continue
        root = Path(ssd) / BATCHES_SUBDIR / batch_name.strip()
        if root.is_dir():
            roots.append(root)
    return roots


def choose_upload_sources(ssd1: str, ssd2: str, batch_name: str) -> list[Path]:
    """Return 0 or 1 local batch folder. Never merge two SSDs into one S3 job."""
    roots = list_local_batch_roots(ssd1, ssd2, batch_name)
    if len(roots) <= 1:
        return roots
    raise RuntimeError(
        f"Both SSDs have {batch_name} ({roots[0]} and {roots[1]}). "
        "Do not use the top Upload this batch button. On the SSD card below, "
        "click Upload this SSD to AWS — one disk, then the other after it finishes."
    )


def resolve_upload_ssds(payload: dict, cfg: dict) -> tuple[str, str]:
    """Pick SSD paths for one upload. A single-SSD request must not fill in the other disk."""
    payload = payload or {}
    cfg = cfg or {}
    slot = str(payload.get("ssd_slot") or "").strip()
    p1 = str(payload.get("ssd1") or "").strip()
    p2 = str(payload.get("ssd2") or "").strip()
    c1 = str(cfg.get("ssd1") or "").strip()
    c2 = str(cfg.get("ssd2") or "").strip()
    if slot in {"1", "ssd1"}:
        return (p1 or c1, "")
    if slot in {"2", "ssd2"}:
        return ("", p2 or c2)
    if p1 and not p2:
        return (p1, "")
    if p2 and not p1:
        return ("", p2)
    return (p1 or c1, p2 or c2)


def size_tolerance_bytes(expected_bytes: int) -> int:
    """1 MiB floor, else 0.01% of expected (intentional for multi-TB listings)."""
    expected = max(0, int(expected_bytes or 0))
    return max(SIZE_TOLERANCE_FLOOR_BYTES, int(expected * SIZE_TOLERANCE_PERCENT))


def sizes_within_tolerance(local_bytes: int, remote_bytes: int) -> bool:
    local_bytes = int(local_bytes or 0)
    remote_bytes = int(remote_bytes or 0)
    return abs(remote_bytes - local_bytes) <= size_tolerance_bytes(local_bytes)


def _count_files(root: Path) -> int:
    n = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                n += 1
    except OSError:
        pass
    return n


def _walk_dir_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _dir_bytes(root: Path, *, recorded_sizes: list[int] | None = None) -> int:
    """Sum file sizes. Prefer a complete recorded list; else walk the folder."""
    if recorded_sizes is not None:
        try:
            recorded = [int(x) for x in recorded_sizes]
        except (TypeError, ValueError):
            recorded = None
        else:
            nfiles = _count_files(root)
            if nfiles > 0 and nfiles == len(recorded):
                return sum(recorded)
    return _walk_dir_bytes(root)


def _next_free_s3_name(stem: str, suffix: str, taken_lower: set[str]) -> str:
    first = f"{stem}{suffix}"
    if first.lower() not in taken_lower:
        return first
    n = 1
    while True:
        cand = f"{stem}-{n}{suffix}"
        if cand.lower() not in taken_lower:
            return cand
        n += 1


def _sidecar_local_stem(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".segments.json"):
        return name[: -len(".segments.json")]
    return Path(name).stem


def plan_s3_dest_names(
    local_files: list[tuple[str, int]],
    s3_files: dict[str, int],
) -> dict[str, str]:
    """Map local filenames → S3 object names. Never overwrite a different-sized key.

    Same name + same size → keep the name (already on S3). Same name + different
    size → ``stem-1``, ``stem-2``, … Sidecars follow the MP4 dest stem.
    """
    s3_lower = {str(k).lower(): (str(k), int(v)) for k, v in (s3_files or {}).items()}
    taken = {str(k).lower() for k in (s3_files or {})}
    mapping: dict[str, str] = {}
    mp4s = [(n, s) for n, s in local_files if Path(n).suffix.upper() == ".MP4"]
    others = [(n, s) for n, s in local_files if Path(n).suffix.upper() != ".MP4"]
    stem_map: dict[str, str] = {}
    for name, size in mp4s:
        existing = s3_lower.get(name.lower())
        if existing is None:
            dest = name
        elif int(existing[1]) == int(size):
            dest = existing[0]
        else:
            dest = _next_free_s3_name(Path(name).stem, Path(name).suffix, taken)
        mapping[name] = dest
        stem_map[Path(name).stem] = Path(dest).stem
        taken.add(dest.lower())
        s3_lower[dest.lower()] = (dest, int(size))
    for name, _size in others:
        local_stem = _sidecar_local_stem(name)
        dest_stem = stem_map.get(local_stem, local_stem)
        suffix = ".segments.json" if name.lower().endswith(".segments.json") else Path(name).suffix
        dest = f"{dest_stem}{suffix}"
        if dest.lower() in taken:
            existing = s3_lower.get(dest.lower())
            local_sz = int(_size)
            if existing and int(existing[1]) != local_sz:
                dest = _next_free_s3_name(dest_stem, suffix, taken)
        mapping[name] = dest
        taken.add(dest.lower())
    return mapping


def _list_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    try:
        for path in root.rglob("*"):
            if path.is_file():
                files.append(path)
    except OSError:
        pass
    return files


def _s3_object_basename(uri_or_key: str) -> str:
    text = str(uri_or_key or "").replace("\\", "/").rstrip("/")
    return Path(text).name


def _parse_s3_ls_sizes(text: str) -> dict[str, int]:
    """Parse ``s5cmd ls`` / ``aws s3 ls --recursive`` into {filename: size}."""
    out: dict[str, int] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        # s5cmd: DATE TIME SIZE s3://bucket/key
        m = re.match(
            r"^\S+\s+\S+\s+(\d+)\s+(s3://\S+|\S+)$",
            line,
        )
        if not m:
            continue
        size = int(m.group(1))
        name = _s3_object_basename(m.group(2))
        if name:
            out[name] = size
    return out


_EMPTY_S3_LS_RE = re.compile(
    r"no object found|no objects found|0 objects|Not Found|The specified key does not exist",
    re.IGNORECASE,
)
_FATAL_S3_LS_RE = re.compile(
    r"AccessDenied|InvalidAccessKey|SignatureDoesNotMatch|ExpiredToken|"
    r"NoSuchBucket|Forbidden|Unable to locate credentials|could not get credentials|"
    r"AccessDeniedException|AllAccessDisabled",
    re.IGNORECASE,
)


_LAST_S3_LIST_ERROR = ""


def _s3_ls_output(result: subprocess.CompletedProcess[str]) -> str:
    return ((result.stdout or "") + "\n" + (result.stderr or "")).strip()


def _run_s3_ls(cmd: list[str]) -> tuple[str, str]:
    """Return ('ok'|'empty'|'timeout'|'error', text)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=AWS_VERIFY_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return "timeout", "S3 listing timed out after 30 minutes"
    except OSError as exc:
        return "error", str(exc)
    text = _s3_ls_output(result)
    if result.returncode == 0:
        return "ok", text
    if _FATAL_S3_LS_RE.search(text):
        return "error", text or f"exit {result.returncode}"
    if _EMPTY_S3_LS_RE.search(text) or not text:
        return "empty", text
    return "error", text or f"exit {result.returncode}"


def list_s3_object_sizes(dest: str) -> dict[str, int] | None:
    """List object basenames under an S3 prefix. None = listing failed/timed out.

    An empty prefix (nothing uploaded yet) is ``{}``, not an error — otherwise
    the first upload of a batch is blocked when s5cmd prints ``no object found``.
    """
    global _LAST_S3_LIST_ERROR
    _LAST_S3_LIST_ERROR = ""
    if not dest.startswith("s3://"):
        _LAST_S3_LIST_ERROR = "destination is not an s3:// URI"
        return None
    prefix = dest if dest.endswith("/") else dest + "/"
    empty_ok = False
    last_error = ""
    if s5cmd_available():
        for target in (prefix, prefix + "*"):
            status, text = _run_s3_ls(["s5cmd", "ls", target])
            if status == "timeout":
                _LAST_S3_LIST_ERROR = text
                return None
            if status == "ok":
                return _parse_s3_ls_sizes(text)
            if status == "empty":
                empty_ok = True
            else:
                last_error = text
    if aws_cli_available():
        status, text = _run_s3_ls(["aws", "s3", "ls", prefix, "--recursive"])
        if status == "timeout":
            _LAST_S3_LIST_ERROR = text
            return None
        if status == "ok":
            return _parse_s3_ls_sizes(text)
        if status == "empty":
            empty_ok = True
        else:
            last_error = text or last_error
    if empty_ok:
        return {}
    _LAST_S3_LIST_ERROR = last_error or "s5cmd/aws listing failed"
    return None


def _prepare_s3_upload_plan(
    local_root: Path, dest: str
) -> tuple[dict[str, str], list[tuple[Path, str]] | None]:
    """Return (local_name→s3_name, optional per-file cp pairs).

    Folder ``sync`` is used when every file keeps its local name. If a name is
    already on S3 at a *different* size, we ``cp`` to ``stem-1`` / ``stem-2``.
    """
    files = _list_source_files(local_root)
    local_rows = []
    for path in files:
        try:
            local_rows.append((path.name, int(path.stat().st_size)))
        except OSError:
            continue
    existing = list_s3_object_sizes(dest)
    if existing is None:
        detail = (_LAST_S3_LIST_ERROR or "").strip()
        extra = f" Details: {detail[:300]}" if detail else ""
        raise RuntimeError(
            "Could not list S3 objects (timeout, login error, or s5cmd/aws failed). "
            "Refusing to upload so existing keys are not overwritten. "
            "Click Test AWS connection, then retry."
            + extra
        )
    key_map = plan_s3_dest_names(local_rows, existing)
    dest_norm = dest if dest.endswith("/") else dest + "/"
    needs_remap = any(key_map.get(name, name) != name for name, _sz in local_rows)
    if not needs_remap:
        return key_map, None
    pairs: list[tuple[Path, str]] = []
    existing_lower = {k.lower(): int(v) for k, v in existing.items()}
    by_name = {p.name: p for p in files}
    for name, size in local_rows:
        src = by_name.get(name)
        if src is None:
            continue
        s3_name = key_map.get(name, name)
        prior = existing_lower.get(s3_name.lower())
        if prior is not None and int(prior) == int(size):
            continue
        pairs.append((src, f"{dest_norm}{s3_name}"))
    return key_map, pairs


def find_running_batch_job(batch_name: str, dest: str | None = None) -> dict | None:
    """Return a running upload for this batch (same S3 dest when provided)."""
    batch = batch_name.strip()
    with _lock:
        for job in _jobs.values():
            if job.get("status") != "running":
                continue
            if str(job.get("batch") or "").strip() != batch:
                continue
            if dest and str(job.get("dest") or "").rstrip("/") != dest.rstrip("/"):
                continue
            return dict(job)
    return None


def start_batch_upload(
    *,
    s3_uri: str,
    batch_name: str,
    ssd1: str,
    ssd2: str,
    card_id: str | None = None,
    external_window: bool = True,
    show_console: bool | None = None,
    auto_delete: bool = False,
) -> dict:
    """Start s5cmd/aws sync in an external console (survives server restart).

    Batches are stored flat on the SSD (``Batches/<batch>/*.MP4``), so we always
    sync the whole batch folder(s) → ``s3://…/<batch>/``. ``card_id`` is only a
    label for "triggered after this card finished" — it must not look for a
    per-card subfolder (that was the old layout and broke SSD+AWS mode).
    """
    del show_console  # always external + logged
    del external_window
    tool = preferred_uploader()
    if not tool:
        raise RuntimeError(
            "Neither s5cmd nor AWS CLI found. Install s5cmd (recommended) or AWS CLI v2, then `aws configure`."
        )

    prefix = batch_s3_prefix(s3_uri, batch_name)
    roots = choose_upload_sources(ssd1, ssd2, batch_name)
    if not roots:
        raise RuntimeError(f"No local batch folder found for {batch_name} on the selected SSDs")

    # Flat layout: one SSD batch folder → ``s3://…/<batch>/`` (no card subfolder).
    sources = roots
    dest = prefix

    # If an upload for this batch is already running, don't open a second CMD
    # racing the same S3 prefix. Mark it to resync when the current job ends
    # so files copied after the sync started still get uploaded.
    running = find_running_batch_job(batch_name, dest)
    if running:
        with _lock:
            job = _jobs.get(running["id"])
            if job and job.get("status") == "running":
                job["pending_resync"] = True
                job["pending_resync_ssd1"] = ssd1
                job["pending_resync_ssd2"] = ssd2
                job["pending_resync_s3_uri"] = s3_uri
                if card_id:
                    job["pending_resync_card_id"] = card_id
                if auto_delete:
                    job["auto_delete"] = True
                job["message"] = (
                    (job.get("message") or "Uploading")
                    + f" · will resync after finish"
                    + (f" (new files from {card_id})" if card_id else "")
                )
                _append_job_log(
                    job,
                    f"Coalesced: another upload requested"
                    + (f" after {card_id}" if card_id else "")
                    + " — queued pending_resync",
                )
        _persist_jobs()
        return get_job(running["id"]) or running

    key_map, file_pairs = _prepare_s3_upload_plan(sources[0], dest)
    job = _launch_upload_job(
        sources=sources,
        dest=dest,
        batch_name=batch_name,
        card_id=card_id,
        s3_uri=s3_uri,
        tool=tool,
        key_map=key_map,
        file_pairs=file_pairs,
        auto_delete=auto_delete,
    )
    if auto_delete:
        with _lock:
            live = _jobs.get(job.get("id") or "")
            if live:
                live["auto_delete"] = True
        _persist_jobs()
        job = get_job(job.get("id") or "") or job
    return job


def cancel_job(job_id: str) -> dict:
    """Stop a running AWS upload (kill CMD / s5cmd / aws process tree)."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        status = str(job.get("status") or "")
        if status == "cancelled":
            return dict(job)
        if status not in {"running", "checking"}:
            raise RuntimeError(f"Job is not running ({status}) — nothing to cancel")
        snap = dict(job)
        job["cancel_requested"] = True
        job["pending_resync"] = False
        job["status"] = "cancelling"
        job["message"] = "Cancel requested — stopping upload processes…"
        _append_job_log(job, "Cancel requested by operator")
    _persist_jobs()

    killed = _kill_upload_processes(snap)
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        job["cancel_requested"] = True
        job["pending_resync"] = False
        job["status"] = "cancelled"
        job["speed_mbps"] = 0.0
        job["eta_seconds"] = None
        job["aws_pid"] = None
        job["message"] = (
            "Cancelled — S3 may have a partial upload; click Retry to resume missing files"
            + (f" · stopped {killed} process(es)" if killed else "")
        )
        _append_job_log(job, f"Cancelled (killed={killed})")
        # Marker so log monitor does not treat a half-written exit as a hard error.
        log_path = Path(str(job.get("log_path") or ""))
        if log_path.is_file():
            try:
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"\n{EXIT_MARKER}cancelled\n")
            except OSError:
                pass
    _persist_jobs()
    return get_job(job_id) or {"id": job_id, "status": "cancelled"}


def _kill_upload_processes(job: dict) -> int:
    """Kill CMD/PowerShell/s5cmd/aws processes tied to this upload job."""
    needles: list[str] = []
    for key in ("script", "log_path", "dest", "run_file"):
        value = str(job.get(key) or "").strip()
        if value:
            needles.append(value)
            needles.append(value.replace("/", "\\"))
            needles.append(value.replace("\\", "/"))
    # Unique basename of the .bat/.sh also helps match the CMD window title path.
    script = str(job.get("script") or "")
    if script:
        needles.append(Path(script).name)
    dest = str(job.get("dest") or "").strip()
    if dest:
        needles.append(dest.rstrip("/"))
    needles = [n for n in dict.fromkeys(needles) if len(n) >= 8]

    pids: set[int] = set()
    stored = job.get("aws_pid")
    if stored:
        try:
            pids.add(int(stored))
        except (TypeError, ValueError):
            pass

    if platform.system() == "Windows":
        pids.update(_windows_pids_matching(needles))
    else:
        pids.update(_posix_pids_matching(needles))

    killed = 0
    for pid in sorted(pids):
        if _kill_pid_tree(pid):
            killed += 1
    return killed


def _windows_pids_matching(needles: list[str]) -> set[int]:
    if not needles:
        return set()
    try:
        ps = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | "
                "Where-Object { $_.Name -match '^(aws|s5cmd|cmd|powershell|pwsh)\\.exe$' } | "
                "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if ps.returncode != 0 or not ps.stdout.strip():
        return set()
    try:
        data = json.loads(ps.stdout)
    except json.JSONDecodeError:
        return set()
    rows = data if isinstance(data, list) else [data]
    found: set[int] = set()
    needles_l = [n.lower() for n in needles]
    for row in rows:
        if not isinstance(row, dict):
            continue
        cmd = str(row.get("CommandLine") or "")
        if not cmd:
            continue
        cmd_l = cmd.lower()
        # Only touch sync-related shells / uploaders.
        name = str(row.get("Name") or "").lower()
        if name in {"aws.exe", "s5cmd.exe"} and "sync" not in cmd_l and " run " not in f" {cmd_l} ":
            continue
        if name in {"cmd.exe", "powershell.exe", "pwsh.exe"}:
            if "sync" not in cmd_l and not any(
                Path(n).name.lower() in cmd_l for n in needles if n.endswith((".bat", ".sh", ".log"))
            ):
                # Still match if dest / script path appears.
                if not any(n in cmd_l for n in needles_l):
                    continue
        if any(n in cmd_l for n in needles_l):
            try:
                found.add(int(row["ProcessId"]))
            except (KeyError, TypeError, ValueError):
                pass
    return found


def _posix_pids_matching(needles: list[str]) -> set[int]:
    found: set[int] = set()
    try:
        out = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return found
    if out.returncode != 0:
        return found
    needles_l = [n.lower() for n in needles]
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        cmd_l = parts[1].lower()
        if not any(tool in cmd_l for tool in ("s5cmd", "aws s3", "aws_upload", ".sh")):
            continue
        if any(n in cmd_l for n in needles_l):
            try:
                found.add(int(parts[0]))
            except ValueError:
                pass
    return found


def _kill_pid_tree(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.returncode == 0
        os.kill(pid, 15)
        time.sleep(0.4)
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        return True
    except (OSError, subprocess.TimeoutExpired, ProcessLookupError):
        return False


def restart_job(job_id: str) -> dict:
    """Re-run sync for a failed/interrupted/mismatched job (resume-safe)."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        if job.get("status") == "running":
            raise RuntimeError("Upload still running — wait for it to finish or close its CMD window")
        sources = [Path(p) for p in (job.get("sources") or []) if p]
        dest = str(job.get("dest") or "").strip()
        batch_name = str(job.get("batch") or "").strip() or "batch"
        card_id = job.get("card_id")
        s3_uri = str(job.get("s3_uri") or "").strip()

    if not sources or not all(p.is_dir() for p in sources):
        raise RuntimeError("Local source folder missing — pick the SSD batch and Upload again")
    if not dest.startswith("s3://"):
        raise RuntimeError("Job is missing an S3 destination")

    # Allow restart from cancelled / error / mismatch states.
    if s3_uri and batch_name:
        dest = batch_s3_prefix(s3_uri, batch_name)

    tool = preferred_uploader()
    if not tool:
        raise RuntimeError("Neither s5cmd nor AWS CLI found")

    # Clear cancel flags from a prior stop.
    with _lock:
        prev = _jobs.get(job_id)
        if prev:
            prev["cancel_requested"] = False

    # Replace this job id so the UI Restart button keeps a stable reference.
    key_map, file_pairs = _prepare_s3_upload_plan(sources[0], dest)
    return _launch_upload_job(
        sources=sources,
        dest=dest,
        batch_name=batch_name,
        card_id=card_id,
        s3_uri=s3_uri,
        tool=tool,
        reuse_job_id=job_id,
        restart=True,
        key_map=key_map,
        file_pairs=file_pairs,
    )


def _auto_delete_enabled(job: dict | None) -> bool:
    """Delete local after verify unless the job explicitly set auto_delete False."""
    if not job:
        return False
    return job.get("auto_delete") is not False


def verify_job_sizes(job_id: str) -> dict:
    """Compare local folder bytes vs S3 prefix; mark verified or mismatch."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        snap = dict(job)

    sources = [Path(p) for p in (snap.get("sources") or []) if p]
    dest = str(snap.get("dest") or "").strip()
    if not sources:
        raise RuntimeError("No local sources stored on this job")
    if not dest.startswith("s3://"):
        raise RuntimeError("No S3 destination on this job")

    key_map = dict(snap.get("key_map") or {})
    recorded = snap.get("recorded_sizes")
    first = _compare_local_s3_sizes(
        sources, dest, key_map=key_map, recorded_sizes=recorded
    )
    listing_failed = first.get("error") in {"timeout", "listing_failed"}
    second = first
    if not listing_failed:
        second = _compare_local_s3_sizes(
            sources, dest, key_map=key_map, recorded_sizes=recorded
        )
        listing_failed = second.get("error") in {"timeout", "listing_failed"}
    result = dict(second)
    if listing_failed:
        result["ok"] = False
    else:
        result["ok"] = bool(
            first.get("ok")
            and second.get("ok")
            and int(first.get("s3_bytes") or 0) == int(second.get("s3_bytes") or 0)
            and int(first.get("local_bytes") or 0) == int(second.get("local_bytes") or 0)
        )
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        job["local_bytes"] = result["local_bytes"]
        job["s3_bytes"] = result["s3_bytes"]
        job["s3_objects"] = result["s3_objects"]
        job["size_delta"] = result["delta"]
        if result["ok"]:
            job["status"] = "verified"
            job["verified"] = True
            job["message"] = (
                f"Verified · this SSD {result['local_bytes']} ≈ S3 keys {result['s3_bytes']} "
                f"({result['s3_objects']} objects)"
                + (
                    " — deleting that batch folder on this SSD"
                    if _auto_delete_enabled(job)
                    else " — safe to delete local if you want"
                )
            )
        else:
            job["verified"] = False
            if listing_failed:
                if job.get("status") == "verified":
                    job["status"] = "completed"
                job["message"] = (
                    "S3 listing timed out or failed after 30 minutes — "
                    "local files NOT deleted. Click Verify sizes to try again."
                )
            else:
                if job.get("status") in {"completed", "verified", "mismatch"}:
                    job["status"] = "mismatch"
                job["message"] = (
                    f"Size mismatch · this SSD {result['local_bytes']} vs its S3 keys "
                    f"{result['s3_bytes']} (Δ {result['delta']}) — click Retry to resume"
                )
        _append_job_log(
            job,
            f"VERIFY local={result['local_bytes']} s3={result['s3_bytes']} "
            f"ok={result['ok']} error={result.get('error')}",
        )
        want_delete = bool(result["ok"] and _auto_delete_enabled(job) and not job.get("followup_resync"))
    _persist_jobs()
    if want_delete:
        try:
            delete_local_after_verify(job_id, confirmed=True)
        except Exception as exc:  # noqa: BLE001
            with _lock:
                live = _jobs.get(job_id)
                if live:
                    live["message"] = f"Verified but delete refused: {exc}"
            _persist_jobs()
    return get_job(job_id) or result


def delete_local_after_verify(job_id: str, *, confirmed: bool = False) -> dict:
    """Delete local SSD sources only after size verification succeeded."""
    if not confirmed:
        raise RuntimeError("Deletion requires confirmed=true")
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise RuntimeError("Upload job not found")
        if job.get("status") != "verified" and not job.get("verified"):
            raise RuntimeError("Verify sizes first — only delete after local ≈ S3")
        sources = [Path(p) for p in (job.get("sources") or []) if p]
        snap = dict(job)

    if not sources:
        raise RuntimeError("No local sources to delete")

    # Re-check this job's mapped keys only — never the whole prefix (other SSD may be there).
    check = _compare_local_s3_sizes(
        sources,
        str(snap.get("dest") or ""),
        key_map=dict(snap.get("key_map") or {}),
        recorded_sizes=snap.get("recorded_sizes"),
    )
    if not check["ok"]:
        listing_failed = check.get("error") in {"timeout", "listing_failed"}
        with _lock:
            job = _jobs.get(job_id)
            if job:
                job["verified"] = False
                if listing_failed:
                    if job.get("status") == "verified":
                        job["status"] = "completed"
                    job["message"] = (
                        "Refusing delete — S3 listing timed out or failed. Local files kept."
                    )
                else:
                    job["status"] = "mismatch"
                    job["message"] = "Refusing delete — sizes no longer match. Restart upload."
        _persist_jobs()
        if listing_failed:
            raise RuntimeError("S3 listing timed out — refusing to delete local files")
        raise RuntimeError("Sizes no longer match — refusing to delete local files")

    deleted: list[str] = []
    errors: list[str] = []
    for src in sources:
        try:
            if src.is_dir():
                shutil.rmtree(src)
                deleted.append(str(src))
        except OSError as exc:
            errors.append(f"{src}: {exc}")

    with _lock:
        job = _jobs.get(job_id)
        if job:
            job["status"] = "deleted_local"
            job["message"] = (
                f"Deleted local after verify ({len(deleted)} folder(s))"
                + (f" · errors: {'; '.join(errors)}" if errors else "")
            )
            _append_job_log(job, f"DELETED {deleted}")
    _persist_jobs()
    if errors and not deleted:
        raise RuntimeError("; ".join(errors))
    batch_name = str(snap.get("batch") or "")
    if _on_batch_deleted:
        try:
            _on_batch_deleted([str(p) for p in sources], batch_name)
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        live = _jobs.get(job_id)
        if live:
            live["delete_hooked"] = True
    _persist_jobs()
    return get_job(job_id) or {"ok": True, "deleted": deleted, "errors": errors}


def _append_job_log(job: dict, line: str) -> None:
    job["log"] = (job.get("log") or [])[-100:] + [line]


def _launch_upload_job(
    *,
    sources: list[Path],
    dest: str,
    batch_name: str,
    card_id: str | None,
    s3_uri: str,
    tool: str,
    reuse_job_id: str | None = None,
    restart: bool = False,
    key_map: dict[str, str] | None = None,
    file_pairs: list[tuple[Path, str]] | None = None,
    auto_delete: bool | None = None,
) -> dict:
    recorded: list[int] = []
    for src in sources:
        for path in _list_source_files(src):
            try:
                recorded.append(int(path.stat().st_size))
            except OSError:
                pass
    total_bytes = sum(
        _dir_bytes(src, recorded_sizes=recorded if len(sources) == 1 else None)
        for src in sources
    )
    stamp = int(time.time())
    label = f"{batch_name}-{card_id or 'ALL'}-{stamp}"
    job_id = reuse_job_id or f"aws:{label}"
    safe = re.sub(r"[^\w.-]+", "_", f"{batch_name}-{card_id or 'ALL'}-{stamp}")

    ensure_dirs()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{safe}.log"
    script_path = LOG_DIR / f"{safe}{'.bat' if platform.system() == 'Windows' else '.sh'}"
    run_file: Path | None = None
    if file_pairs:
        run_file = LOG_DIR / f"{safe}.s5cmd.txt"
        run_file.write_text(
            "\n".join(
                f'cp "{src}" "{s3_uri_key}"' for src, s3_uri_key in file_pairs
            )
            + "\n",
            encoding="utf-8",
        )

    workers = _numworkers()
    retries = _upload_retries()
    header = (
        f"AWS S3 upload  {batch_name}"
        + (f" / {card_id}" if card_id else "")
        + f"\nTool: {tool}"
        + (
            f" · try default sync first, then --numworkers {workers} on failure"
            if tool == "s5cmd"
            else ""
        )
        + f"\nRetries: {retries}"
        + f"\nDestination: {dest}\n"
        f"Local size: {total_bytes} bytes\n"
        "This CMD window keeps uploading even if you restart the offloader.\n"
        "============================================\n"
    )
    log_path.write_text(header, encoding="utf-8")

    _write_external_script(
        script_path,
        sources=sources,
        dest=dest,
        log_path=log_path,
        title=f"AWS — {batch_name}",
        tool=tool,
        numworkers=workers,
        retries=retries,
        run_file=run_file,
    )
    _launch_external_script(
        script_path,
        title=f"AWS upload — {batch_name}" + (f" / {card_id}" if card_id else ""),
    )

    message = (
        f"{'Restarted' if restart else 'CMD'} {tool} upload → {dest}"
        + (
            f" (default sync, then workers={workers} on fail · retries={retries})"
            if tool == "s5cmd"
            else f" (retries={retries})"
        )
    )
    with _lock:
        prev = _jobs.get(job_id) if reuse_job_id else None
        _jobs[job_id] = {
            "id": job_id,
            "status": "running",
            "batch": batch_name,
            "card_id": card_id,
            "dest": dest,
            "s3_uri": s3_uri,
            "uploader": tool,
            "numworkers": workers if tool == "s5cmd" else None,
            "retries": retries,
            "bytes_done": int(prev.get("bytes_done") or 0) if prev else 0,
            "bytes_total": total_bytes,
            "files_done": 0,
            "speed_mbps": 0.0,
            "eta_seconds": None,
            "message": message,
            "log": [f"Local size {total_bytes} bytes", f"Script {script_path}", f"Tool {tool}"],
            "started_at": time.time(),
            "external": True,
            "console": True,
            "log_path": str(log_path),
            "script": str(script_path),
            "sources": [str(s) for s in sources],
            "log_offset": 0,
            "using_completed_meter": False,
            "transferred": 0,
            "verified": False,
            "progress_via_s3": True,
            "auto_delete": (
                True
                if auto_delete is None and not prev
                else bool(auto_delete)
                if auto_delete is not None
                else prev.get("auto_delete") is not False
            ),
            "key_map": dict(key_map or {}),
            "recorded_sizes": recorded,
            "run_file": str(run_file) if run_file else None,
        }
    _persist_jobs()
    _ensure_monitor()
    return get_job(job_id) or {"id": job_id, "status": "running"}


def _write_external_script(
    script_path: Path,
    *,
    sources: list[Path],
    dest: str,
    log_path: Path,
    title: str,
    tool: str = "aws",
    numworkers: int = 20,
    retries: int = 5,
    run_file: Path | None = None,
) -> None:
    """Write a console script that syncs with auto-retry and tees output into log_path.

    For s5cmd: first try plain ``s5cmd sync`` (faster default workers). If that
    fails, later retries use ``s5cmd --numworkers N`` (helps flaky multipart links).
    """
    if platform.system() == "Windows":
        lines = [
            "@echo off",
            "setlocal EnableDelayedExpansion",
            "chcp 65001 >nul",
            f"title {title}",
            "echo ============================================",
            f"echo   {title}",
            f"echo   Tool: {tool}",
            (
                f"echo   Strategy: plain s5cmd sync first, then --numworkers {numworkers} if it fails"
                if tool == "s5cmd"
                else "echo   Strategy: aws s3 sync with retries"
            ),
            f"echo   Destination: {dest}",
            f"echo   Auto-retries: {retries}",
            "echo   Progress also appears in the offloader web UI.",
            "echo   Closing this window STOPS the upload.",
            "echo   Restarting the offloader does NOT stop this window.",
            "echo ============================================",
            "echo.",
        ]
        dest_norm = dest if dest.endswith("/") else dest + "/"
        log_q = f'"{log_path}"'
        if run_file is not None:
            run_q = f'"{run_file}"'
            if tool == "s5cmd":
                sync_default = f"s5cmd run {run_q}"
                sync_workers = f"s5cmd --numworkers {numworkers} run {run_q}"
            else:
                sync_default = f"s5cmd run {run_q}" if s5cmd_available() else f"aws s3 sync {_sync_local_arg(sources[0])} {dest_norm}"
                sync_workers = sync_default
            lines.append(f"echo Uploading remapped keys from {run_q}")
            lines.append(f"echo Uploading remapped keys from {run_q}>> {log_q}")
            lines.append(f"set MAX_TRIES={retries}")
            lines.append("set TRY=1")
            lines.append(":retry_loop_0")
            lines.append("echo --- attempt !TRY! of %MAX_TRIES% ---")
            lines.append(f"echo --- attempt !TRY! of %MAX_TRIES% --->> {log_q}")
            lines.append("if !TRY! equ 1 (")
            lines.append(f"  {sync_default}")
            lines.append(") else (")
            lines.append(f"  {sync_workers}")
            lines.append(")")
            lines.append("set SYNC_ERR=%ERRORLEVEL%")
            lines.append(f"echo Sync exit !SYNC_ERR!>> {log_q}")
            lines.append("if %SYNC_ERR% equ 0 goto sync_ok_0")
            lines.append("echo Retrying after connection/upload error (exit %SYNC_ERR%)...")
            lines.append(f"echo Retrying after connection/upload error (exit %SYNC_ERR%)>> {log_q}")
            lines.append("timeout /t 15 /nobreak >nul")
            lines.append("set /a TRY+=1")
            lines.append("if !TRY! leq %MAX_TRIES% goto retry_loop_0")
            lines.append(f"echo {EXIT_MARKER}%SYNC_ERR%>> {log_q}")
            lines.append("echo.")
            lines.append("echo ERROR: sync failed after retries. Click Retry in the UI.")
            lines.append("pause")
            lines.append("exit /b %SYNC_ERR%")
            lines.append(":sync_ok_0")
            lines.append("echo.")
        else:
            for idx, src in enumerate(sources):
                src_arg = _sync_local_arg(src)
                # Prefer quoted paths for cmd.exe (spaces in "batch 1").
                src_q = f'"{src_arg}"'
                dest_q = f'"{dest_norm}"'
                log_q = f'"{log_path}"'
                if tool == "s5cmd":
                    sync_default = f"s5cmd sync {src_q} {dest_q}"
                    sync_workers = f"s5cmd --numworkers {numworkers} sync {src_q} {dest_q}"
                else:
                    sync_default = f"aws s3 sync {src_q} {dest_q}"
                    sync_workers = sync_default
                lines.append(f"echo Syncing {src_q} → {dest_q}")
                lines.append(f"echo Syncing {src_q} → {dest_q}>> {log_q}")
                lines.append(f"set MAX_TRIES={retries}")
                lines.append("set TRY=1")
                lines.append(f":retry_loop_{idx}")
                lines.append("echo --- attempt !TRY! of %MAX_TRIES% ---")
                lines.append(f"echo --- attempt !TRY! of %MAX_TRIES% --->> {log_q}")
                lines.append("if !TRY! equ 1 (")
                lines.append(
                    "  echo Using default s5cmd sync" if tool == "s5cmd" else "  echo Using aws s3 sync"
                )
                # Run in this CMD window so progress is visible; UI also tracks via S3 size.
                lines.append(f"  {sync_default}")
                lines.append(") else (")
                if tool == "s5cmd":
                    lines.append(f"  echo Using s5cmd --numworkers {numworkers} sync")
                else:
                    lines.append("  echo Retrying aws s3 sync")
                lines.append(f"  {sync_workers}")
                lines.append(")")
                lines.append("set SYNC_ERR=%ERRORLEVEL%")
                lines.append(f"echo Sync exit !SYNC_ERR!>> {log_q}")
                lines.append(f"if %SYNC_ERR% equ 0 goto sync_ok_{idx}")
                lines.append("echo Retrying after connection/upload error (exit %SYNC_ERR%)...")
                lines.append(f"echo Retrying after connection/upload error (exit %SYNC_ERR%)>> {log_q}")
                lines.append("timeout /t 15 /nobreak >nul")
                lines.append("set /a TRY+=1")
                lines.append(f"if !TRY! leq %MAX_TRIES% goto retry_loop_{idx}")
                lines.append(f"echo {EXIT_MARKER}%SYNC_ERR%>> {log_q}")
                lines.append("echo.")
                lines.append("echo ERROR: sync failed after retries. Click Retry in the UI.")
                lines.append("pause")
                lines.append("exit /b %SYNC_ERR%")
                lines.append(f":sync_ok_{idx}")
                lines.append("echo.")
        lines.append(f'echo {EXIT_MARKER}0>> "{log_path}"')
        lines.append("echo ============================================")
        lines.append("echo   Upload finished OK — UI will verify sizes next")
        lines.append("echo ============================================")
        lines.append("timeout /t 8 /nobreak >nul")
        script_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    else:
        lines = [
            "#!/bin/bash",
            f'echo "============================================"',
            f'echo "  {title}"',
            f'echo "  Tool: {tool}"',
            (
                f'echo "  Strategy: plain s5cmd sync first, then --numworkers {numworkers} if it fails"'
                if tool == "s5cmd"
                else 'echo "  Strategy: aws s3 sync with retries"'
            ),
            f'echo "  Destination: {dest}"',
            f'echo "  Auto-retries: {retries}"',
            'echo "  Progress also appears in the offloader web UI."',
            'echo "============================================"',
            "echo",
        ]
        dest_norm = dest if dest.endswith("/") else dest + "/"
        if run_file is not None:
            sync_default = f's5cmd run "{run_file}"'
            sync_workers = f's5cmd --numworkers {numworkers} run "{run_file}"'
            lines.append(f'echo "Uploading remapped keys from {run_file}"')
            lines.append("MAX_TRIES={retries}".format(retries=retries))
            lines.append("TRY=1")
            lines.append("while true; do")
            lines.append('  echo "--- attempt $TRY of $MAX_TRIES ---"')
            lines.append('  if [[ "$TRY" -eq 1 ]]; then')
            lines.append(f'    set +e; {sync_default} 2>&1 | tee -a "{log_path}"; ec=${{PIPESTATUS[0]}}; set -e')
            lines.append("  else")
            lines.append(f'    set +e; {sync_workers} 2>&1 | tee -a "{log_path}"; ec=${{PIPESTATUS[0]}}; set -e')
            lines.append("  fi")
            lines.append('  if [[ "$ec" -eq 0 ]]; then break; fi')
            lines.append('  echo "Retrying after error (exit $ec)..."')
            lines.append("  sleep 15")
            lines.append("  TRY=$((TRY+1))")
            lines.append('  if [[ "$TRY" -gt "$MAX_TRIES" ]]; then')
            lines.append(f'    echo "{EXIT_MARKER}${{ec}}" >> "{log_path}"')
            lines.append('    echo "ERROR: sync failed after retries"')
            lines.append("    read -r")
            lines.append('    exit "$ec"')
            lines.append("  fi")
            lines.append("done")
            lines.append("echo")
        else:
            for src in sources:
                src_arg = _sync_local_arg(src)
                if tool == "s5cmd":
                    sync_default = f's5cmd sync "{src_arg}" "{dest_norm}"'
                    sync_workers = (
                        f's5cmd --numworkers {numworkers} sync "{src_arg}" "{dest_norm}"'
                    )
                else:
                    sync_default = f'aws s3 sync "{src_arg}" "{dest_norm}"'
                    sync_workers = sync_default
                lines.append(f'echo "Syncing {src} → {dest_norm}"')
                lines.append(f"MAX_TRIES={retries}")
                lines.append("TRY=1")
                lines.append("while true; do")
                lines.append('  echo "--- attempt $TRY of $MAX_TRIES ---"')
                lines.append('  if [[ "$TRY" -eq 1 ]]; then')
                lines.append(f'    echo "Using default sync"')
                lines.append(f'    set +e; {sync_default} 2>&1 | tee -a "{log_path}"; ec=${{PIPESTATUS[0]}}; set -e')
                lines.append("  else")
                if tool == "s5cmd":
                    lines.append(f'    echo "Using s5cmd --numworkers {numworkers}"')
                else:
                    lines.append('    echo "Retrying aws s3 sync"')
                lines.append(f'    set +e; {sync_workers} 2>&1 | tee -a "{log_path}"; ec=${{PIPESTATUS[0]}}; set -e')
                lines.append("  fi")
                lines.append('  if [[ "$ec" -eq 0 ]]; then break; fi')
                lines.append('  echo "Retrying after error (exit $ec)..."')
                lines.append("  sleep 15")
                lines.append("  TRY=$((TRY+1))")
                lines.append('  if [[ "$TRY" -gt "$MAX_TRIES" ]]; then')
                lines.append(f'    echo "{EXIT_MARKER}${{ec}}" >> "{log_path}"')
                lines.append('    echo "ERROR: sync failed after retries"')
                lines.append("    read -r")
                lines.append('    exit "$ec"')
                lines.append("  fi")
                lines.append("done")
                lines.append("echo")
        lines.append(f'echo "{EXIT_MARKER}0" >> "{log_path}"')
        lines.extend(
            [
                'echo "============================================"',
                'echo "  Upload finished OK — UI will verify sizes next"',
                'echo "============================================"',
                "sleep 5",
            ]
        )
        script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script_path.chmod(0o755)


def _launch_external_script(script_path: Path, *, title: str) -> None:
    system = platform.system()
    if system == "Windows":
        # Detached visible console — survives when Flask/python exits.
        subprocess.Popen(
            ["cmd.exe", "/c", "start", title, "cmd.exe", "/k", str(script_path)],
            cwd=str(STATE_DIR),
            close_fds=True,
        )
        return
    if system == "Darwin":
        escaped = str(script_path).replace('"', '\\"')
        subprocess.Popen(
            ["osascript", "-e", f'tell application "Terminal" to do script "bash \\"{escaped}\\""']
        )
        return
    for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
        if shutil.which(term):
            subprocess.Popen([term, "-e", f"bash {script_path}"])
            return
    raise RuntimeError("No terminal found to show AWS progress")


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def list_jobs() -> list[dict]:
    with _lock:
        return [
            dict(j)
            for j in sorted(_jobs.values(), key=lambda x: x.get("started_at", 0), reverse=True)
        ]


def restore_jobs_from_disk() -> None:
    """Reload jobs and keep monitoring any CMD uploads that are still running."""
    ensure_dirs()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if JOBS_FILE.exists():
        try:
            rows = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            rows = []
        if isinstance(rows, list):
            pending: list[dict] = []
            for row in rows[:40]:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                pending.append(dict(row))
            # Resolve PIDs outside the lock (can be slow).
            pid_live_map = {}
            for job in pending:
                pid = job.get("aws_pid")
                if pid is not None:
                    try:
                        pid_live_map[int(pid)] = _pid_alive(int(pid))
                    except (TypeError, ValueError):
                        pass
            with _lock:
                for job in pending:
                    log_path = Path(str(job.get("log_path") or ""))
                    still_log = _log_still_active(log_path)
                    pid = job.get("aws_pid")
                    pid_live = bool(pid) and pid_live_map.get(int(pid), False)

                    if job.get("status") in {"running", "interrupted", "checking"}:
                        if still_log or pid_live:
                            job["status"] = "running"
                            job["message"] = (
                                "Re-attached after server restart — CMD upload still running"
                            )
                            job["console"] = True
                            job["external"] = True
                            job["progress_via_s3"] = True
                        elif log_path.is_file() and _log_has_exit(log_path):
                            code = _log_exit_code(log_path)
                            if code == 0:
                                job["status"] = "completed"
                                job["bytes_done"] = job.get("bytes_total") or job.get("bytes_done") or 0
                                job["message"] = f"Uploaded to {job.get('dest') or 'S3'}"
                                if not job.get("verified"):
                                    threading.Thread(
                                        target=_auto_verify_job,
                                        args=(job["id"],),
                                        daemon=True,
                                        name=f"aws-verify-restore-{job['id'][-8:]}",
                                    ).start()
                            else:
                                job["status"] = "error"
                                job["message"] = f"Sync failed (exit {code}) — click Retry"
                            job["speed_mbps"] = 0.0
                            job["eta_seconds"] = None
                        else:
                            # May still be uploading in CMD — confirm via process scan next.
                            job["status"] = "checking"
                            job["message"] = "Checking whether CMD upload is still running…"
                            job["speed_mbps"] = 0.0
                    _jobs[job["id"]] = job

    _discover_orphan_logs()
    _persist_jobs()
    _ensure_monitor()

    def _later_discover() -> None:
        try:
            _discover_live_aws_processes()
            _finalize_checking_jobs()
            _persist_jobs()
        except Exception:  # noqa: BLE001
            pass

    # PowerShell WMI process scan can hang — never do it on the request/startup path.
    threading.Thread(target=_later_discover, daemon=True, name="aws-discover").start()


def _finalize_checking_jobs() -> None:
    """After process discovery, mark truly-dead jobs interrupted."""
    with _lock:
        snapshots = [
            (jid, dict(job))
            for jid, job in _jobs.items()
            if job.get("status") == "checking"
        ]
    for job_id, snap in snapshots:
        pid = snap.get("aws_pid")
        if pid and _pid_alive(int(pid)):
            with _lock:
                job = _jobs.get(job_id)
                if job:
                    job["status"] = "running"
                    job["message"] = "CMD upload still running — tracking progress via S3"
            continue
        batch = snap.get("batch")
        dest = snap.get("dest")
        with _lock:
            job = _jobs.get(job_id)
            if not job or job.get("status") != "checking":
                continue
            covered = any(
                other.get("status") == "running"
                and other is not job
                and (
                    (dest and other.get("dest") == dest)
                    or (batch and other.get("batch") == batch)
                )
                for other in _jobs.values()
            )
            if covered:
                job["status"] = "completed"
                job["message"] = "Superseded by live CMD upload tracker"
                continue
            job["status"] = "interrupted"
            job["message"] = (
                "No live upload found — click Retry to resume "
                "(s5cmd/aws sync skips files already on S3)"
            )

def _log_has_exit(log_path: Path) -> bool:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return EXIT_MARKER in text


def _log_exit_code(log_path: Path) -> int:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    code = 1
    for line in text.splitlines():
        if line.startswith(EXIT_MARKER):
            try:
                code = int(line.split(":", 1)[1].strip() or "1")
            except ValueError:
                code = 1
    return code


def _log_still_active(log_path: Path) -> bool:
    if not log_path or not log_path.is_file():
        return False
    if _log_has_exit(log_path):
        return False
    try:
        age = time.time() - log_path.stat().st_mtime
    except OSError:
        return False
    # Still writing, or CMD open mid-file with a quiet stretch — keep watching for a while.
    return age < 6 * 3600


def _discover_orphan_logs() -> None:
    """Pick up log files from CMD uploads if jobs.json was lost."""
    if not LOG_DIR.is_dir():
        return
    with _lock:
        known_logs = {str(Path(j.get("log_path") or "")) for j in _jobs.values()}
    for log_path in sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True):
        if str(log_path) in known_logs:
            continue
        if not _log_still_active(log_path):
            continue
        batch = "unknown"
        try:
            first = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[:3]
            for line in first:
                if line.startswith("AWS S3 upload"):
                    batch = line.replace("AWS S3 upload", "").strip() or batch
        except OSError:
            pass
        job_id = f"aws:reattach:{log_path.stem}"
        with _lock:
            if job_id in _jobs:
                continue
            _jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "batch": batch,
                "card_id": None,
                "dest": "",
                "bytes_done": 0,
                "bytes_total": 0,
                "files_done": 0,
                "speed_mbps": 0.0,
                "eta_seconds": None,
                "message": "Re-attached to existing CMD upload log",
                "log": [],
                "started_at": log_path.stat().st_mtime,
                "external": True,
                "console": True,
                "log_path": str(log_path),
                "log_offset": 0,
                "using_completed_meter": False,
                "transferred": 0,
            }


def _parse_sync_cmdline(cmd: str) -> tuple[str | None, str | None, str | None]:
    """Return (local_source, s3_dest, batch_name) from an aws/s5cmd sync command line."""
    match = _SYNC_ARGS_RE.search(cmd or "")
    if not match:
        return None, None, None
    src = (match.group(1) or match.group(2) or match.group(3) or "").strip().rstrip("\\/")
    dest = (match.group(4) or match.group(5) or match.group(6) or "").strip()
    if dest and not dest.endswith("/"):
        dest += "/"
    batch = None
    if src:
        bm = _BATCH_IN_PATH_RE.search(src)
        if bm:
            batch = bm.group(1).strip().rstrip("\\/")
    if not batch and dest:
        parts = [p for p in dest.rstrip("/").split("/") if p]
        if parts:
            batch = parts[-1]
    return src or None, dest or None, batch


def _parse_s5cmd_du_output(text: str) -> tuple[int, int] | None:
    """Parse ``s5cmd du`` stdout into (bytes, objects)."""
    text = text or ""
    if not _S5CMD_DU_RE.search(text):
        return None
    raw = re.search(
        r"(\d+)\s+bytes\s+in\s+(\d+)\s+objects?",
        text,
        re.IGNORECASE,
    )
    if raw:
        return int(raw.group(1)), int(raw.group(2))
    human = re.search(
        r"([\d.]+)\s*([KMGT]i?B)?\s+in\s+(\d+)\s+objects?",
        text,
        re.IGNORECASE,
    )
    if not human:
        return None
    val = float(human.group(1))
    unit = (human.group(2) or "B").upper()
    objects = int(human.group(3))
    return _to_bytes(val, unit), objects


def _s5cmd_du_targets(dest: str) -> list[str]:
    """Folder URI first; wildcard next — ``du s3://…/prefix/`` often reports 0."""
    base = dest.strip()
    if not base.endswith("/"):
        base = base + "/"
    return [base, base + "*"]


def _run_s5cmd_du(dest: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            ["s5cmd", "du", dest],
            capture_output=True,
            text=True,
            timeout=AWS_VERIFY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _parse_s5cmd_du_output((result.stdout or "") + "\n" + (result.stderr or ""))


def _run_aws_prefix_summary(dest: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            ["aws", "s3", "ls", dest, "--recursive", "--summarize"],
            capture_output=True,
            text=True,
            timeout=AWS_VERIFY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    size_m = _TOTAL_SIZE_RE.search(text)
    obj_m = _TOTAL_OBJECTS_RE.search(text)
    if not size_m:
        return None
    size = int(size_m.group(1))
    objects = int(obj_m.group(1)) if obj_m else 0
    return size, objects


def _s3_prefix_summary(dest: str) -> tuple[int, int] | None:
    """Return (total_bytes, total_objects) already on S3 under dest, or None.

    ``s5cmd du`` on a trailing-slash prefix often prints ``0 bytes in 0 objects``
    even when the files are there. Do not treat that 0 as truth — try the
    wildcard form, then ``aws s3 ls --summarize``.
    """
    if not dest.startswith("s3://"):
        return None
    if s5cmd_available():
        for target in _s5cmd_du_targets(dest):
            parsed = _run_s5cmd_du(target)
            if parsed and parsed[0] > 0:
                return parsed
    if aws_cli_available():
        parsed = _run_aws_prefix_summary(dest)
        if parsed is not None:
            return parsed
    return None


def _s3_bytes_for_names(dest: str, dest_names: set[str]) -> tuple[int, int] | None:
    """Sum S3 sizes for this job's object names. Extra prefix objects are ignored."""
    listed = list_s3_object_sizes(dest)
    if listed is None:
        return None
    want = {str(n).lower() for n in dest_names}
    total = 0
    count = 0
    for name, size in listed.items():
        if str(name).lower() in want:
            total += int(size)
            count += 1
    return total, count


def _compare_local_s3_sizes(
    sources: list[Path],
    dest: str,
    *,
    key_map: dict[str, str] | None = None,
    recorded_sizes: list[int] | None = None,
) -> dict:
    """Compare this job's local files to their mapped S3 keys (not the whole prefix)."""
    rec = recorded_sizes if len(sources) == 1 else None
    local_bytes = sum(
        _dir_bytes(src, recorded_sizes=rec) for src in sources if src.exists()
    )
    listed = list_s3_object_sizes(dest)
    if listed is None:
        return {
            "ok": False,
            "local_bytes": local_bytes,
            "s3_bytes": None,
            "s3_objects": None,
            "delta": None,
            "error": "timeout",
        }
    s3_lower = {str(k).lower(): int(v) for k, v in listed.items()}
    mapping = dict(key_map or {})
    files: list[Path] = []
    for src in sources:
        if src.exists():
            files.extend(_list_source_files(src))
    job_s3_bytes = 0
    job_s3_objects = 0
    missing = 0
    mismatched = 0
    for path in files:
        try:
            size = int(path.stat().st_size)
        except OSError:
            continue
        dest_name = mapping.get(path.name, path.name)
        remote = s3_lower.get(dest_name.lower())
        if remote is None:
            missing += 1
            continue
        job_s3_objects += 1
        job_s3_bytes += int(remote)
        if int(remote) != int(size):
            mismatched += 1
    delta = abs(int(job_s3_bytes) - int(local_bytes))
    ok = (
        missing == 0
        and mismatched == 0
        and job_s3_objects == len(files)
        and sizes_within_tolerance(local_bytes, job_s3_bytes)
    )
    return {
        "ok": ok,
        "local_bytes": local_bytes,
        "s3_bytes": job_s3_bytes,
        "s3_objects": job_s3_objects,
        "delta": delta,
        "error": None if ok else "mismatch",
    }


def set_batch_deleted_hook(fn) -> None:
    global _on_batch_deleted
    _on_batch_deleted = fn


def _auto_verify_job(job_id: str) -> None:
    """Background size check after a successful sync exit; optional auto-delete."""
    with _lock:
        job = dict(_jobs.get(job_id) or {})
    if job.get("followup_resync"):
        return
    try:
        verify_job_sizes(job_id)
    except Exception:  # noqa: BLE001
        with _lock:
            job = _jobs.get(job_id)
            if job and job.get("status") == "completed":
                job["message"] = (
                    (job.get("message") or "Uploaded")
                    + " — click Verify sizes to confirm before deleting local"
                )
        _persist_jobs()


def watchdog_pass() -> list[str]:
    """Resume failed/interrupted uploads; verify completed; never stop a healthy job."""
    notes: list[str] = []
    jobs = list_jobs()
    for job in jobs:
        jid = str(job.get("id") or "")
        status = str(job.get("status") or "")
        if not jid:
            continue
        if status in {"running", "checking", "cancelling"}:
            continue
        if job.get("followup_resync"):
            continue
        if status in {"error", "mismatch", "interrupted"}:
            try:
                restart_job(jid)
                notes.append(f"resumed AWS {jid} ({status})")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"AWS {jid} resume skipped: {exc}")
            continue
        if status == "completed" and not job.get("verified"):
            try:
                _auto_verify_job(jid)
                notes.append(f"verified AWS {jid}")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"AWS {jid} verify failed: {exc}")
            continue
        if status == "verified" and _auto_delete_enabled(job):
            try:
                delete_local_after_verify(jid, confirmed=True)
                notes.append(f"deleted verified AWS {jid}")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"AWS {jid} delete skipped: {exc}")
            continue
        if status == "deleted_local" and not job.get("delete_hooked"):
            if _on_batch_deleted:
                try:
                    _on_batch_deleted(
                        [str(p) for p in (job.get("sources") or [])],
                        str(job.get("batch") or ""),
                    )
                    with _lock:
                        live = _jobs.get(jid)
                        if live:
                            live["delete_hooked"] = True
                    _persist_jobs()
                    notes.append(f"unfroze disk after AWS {jid}")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"AWS {jid} unfreeze failed: {exc}")
    return notes


def _run_pending_resync(
    *,
    s3_uri: str,
    batch_name: str,
    ssd1: str,
    ssd2: str,
    card_id: str | None = None,
) -> None:
    """Start a follow-up full-batch sync after files arrived mid-upload."""
    try:
        # Brief pause so the just-finished CMD releases handles / S3 listings settle.
        time.sleep(2)
        # Fall back to configured SSDs if coalesce didn't stash paths.
        if not ssd1 and not ssd2:
            cfg = load_config()
            ssd1 = str(cfg.get("ssd1") or "")
            ssd2 = str(cfg.get("ssd2") or "")
        job = start_batch_upload(
            s3_uri=s3_uri,
            batch_name=batch_name,
            ssd1=ssd1,
            ssd2=ssd2,
            card_id=card_id,
            auto_delete=True,
        )
        with _lock:
            # Keep a breadcrumb on the new job.
            live = _jobs.get(job.get("id") or "")
            if live:
                _append_job_log(live, "Follow-up resync after mid-upload card dump")
        _persist_jobs()
    except Exception as exc:  # noqa: BLE001
        with _lock:
            for job in _jobs.values():
                if str(job.get("batch") or "") == batch_name.strip():
                    _append_job_log(job, f"Pending resync failed to start: {exc}")
                    break
        _persist_jobs()


def _discover_live_aws_processes() -> None:
    """Detect aws/s5cmd sync still running in CMD (including pre-log older uploads)."""
    if platform.system() != "Windows":
        return
    try:
        ps = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"name='aws.exe' OR name='s5cmd.exe'\" | "
                "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if ps.returncode != 0 or not ps.stdout.strip():
        return
    try:
        data = json.loads(ps.stdout)
    except json.JSONDecodeError:
        return
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if not isinstance(row, dict):
            continue
        cmd = str(row.get("CommandLine") or "")
        pid = row.get("ProcessId")
        name = str(row.get("Name") or "").lower()
        cmd_l = cmd.lower()
        if "sync" not in cmd_l:
            continue
        if "aws" in name and "s3" not in cmd_l:
            continue
        if "s5cmd" in name and "sync" not in cmd_l:
            continue
        src, dest, batch = _parse_sync_cmdline(cmd)
        # Never walk multi-TB trees here — that blocked server startup for minutes.
        bytes_total = 0

        with _lock:
            # Prefer an existing job for same batch / dest / pid (revive interrupted).
            # Never revive an operator-cancelled job.
            existing_id = None
            for jid, j in _jobs.items():
                if j.get("status") in {"cancelled", "cancelling"} or j.get("cancel_requested"):
                    if j.get("aws_pid") == pid or (dest and j.get("dest") == dest):
                        # Same upload was cancelled — do not re-attach.
                        existing_id = None
                        break
                    continue
                if j.get("status") not in {"running", "interrupted", "checking"}:
                    continue
                if j.get("aws_pid") == pid:
                    existing_id = jid
                    break
                if dest and j.get("dest") == dest:
                    existing_id = jid
                    break
                if batch and j.get("batch") == batch:
                    existing_id = jid
                    break
            # Skip creating a tracker if this dest/batch was just cancelled.
            cancelled_match = any(
                (j.get("status") in {"cancelled", "cancelling"} or j.get("cancel_requested"))
                and (
                    (dest and j.get("dest") == dest)
                    or (batch and j.get("batch") == batch)
                    or j.get("aws_pid") == pid
                )
                for j in _jobs.values()
            )
            if cancelled_match and not existing_id:
                continue
            uploader = "s5cmd" if "s5cmd" in name or "s5cmd" in cmd_l else "aws"
            if existing_id:
                job = _jobs[existing_id]
                job["status"] = "running"
                job["aws_pid"] = pid
                job["console"] = True
                job["external"] = True
                job["progress_via_s3"] = True
                job["uploader"] = uploader
                if src and not job.get("sources"):
                    job["sources"] = [src]
                if dest:
                    job["dest"] = dest
                if batch and (
                    not job.get("batch")
                    or "s3:" in str(job.get("batch"))
                    or str(job.get("batch")).startswith("pid-")
                ):
                    job["batch"] = batch
                if bytes_total and not job.get("bytes_total"):
                    job["bytes_total"] = bytes_total
                job["message"] = (
                    f"Live {uploader} upload (PID {pid}"
                    + (f", {batch}" if batch else "")
                    + ") — tracking progress via S3"
                )
                if cmd and not job.get("log"):
                    job["log"] = [cmd[:400]]
                continue

            job_id = f"aws:proc:{pid}"
            if job_id in _jobs:
                job = _jobs[job_id]
                job["status"] = "running"
                job["aws_pid"] = pid
                job["console"] = True
                job["external"] = True
                job["progress_via_s3"] = True
                job["uploader"] = uploader
                if src:
                    job["sources"] = [src]
                if dest:
                    job["dest"] = dest
                if batch:
                    job["batch"] = batch
                if bytes_total:
                    job["bytes_total"] = bytes_total
                job["message"] = (
                    f"Live {uploader} upload (PID {pid}"
                    + (f", {batch}" if batch else "")
                    + ") — tracking progress via S3"
                )
                if cmd:
                    job["log"] = [cmd[:400]] + list(job.get("log") or [])[:20]
                continue
            _jobs[job_id] = {
                "id": job_id,
                "status": "running",
                "batch": batch or f"pid-{pid}",
                "card_id": None,
                "dest": dest or "",
                "bytes_done": 0,
                "bytes_total": bytes_total,
                "files_done": 0,
                "speed_mbps": 0.0,
                "eta_seconds": None,
                "message": (
                    f"Live {uploader} upload (PID {pid}"
                    + (f", {batch}" if batch else "")
                    + ") — measuring progress via S3 (safe to leave CMD open)"
                ),
                "log": [cmd[:400]],
                "started_at": time.time(),
                "external": True,
                "console": True,
                "aws_pid": pid,
                "uploader": uploader,
                "sources": [src] if src else [],
                "log_path": "",
                "log_offset": 0,
                "using_completed_meter": False,
                "transferred": 0,
                "progress_via_s3": True,
                "last_s3_poll": 0.0,
                "last_s3_bytes": 0,
            }


def _persist_jobs() -> None:
    ensure_dirs()
    with _lock:
        rows = []
        for job in sorted(_jobs.values(), key=lambda x: x.get("started_at", 0), reverse=True)[:40]:
            row = dict(job)
            row["log"] = list(row.get("log") or [])[-40:]
            # Keep sources so Restart / Verify / Delete still work after server restart.
            rows.append(row)
    try:
        JOBS_FILE.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except OSError:
        pass


def _ensure_monitor() -> None:
    global _monitor_started
    with _lock:
        if _monitor_started:
            return
        _monitor_started = True
    threading.Thread(target=_monitor_loop, daemon=True, name="aws-log-monitor").start()


def _monitor_loop() -> None:
    ticks = 0
    while True:
        ticks += 1
        # Keep each step isolated — a log-parse bug must not block S3 progress.
        try:
            _poll_all_jobs()
        except Exception:  # noqa: BLE001
            pass
        try:
            _poll_s3_progress_for_jobs()
        except Exception:  # noqa: BLE001
            pass
        if platform.system() == "Windows":
            try:
                _refresh_process_only_jobs()
                if ticks % 5 == 0:
                    _discover_live_aws_processes()
                    _finalize_checking_jobs()
                    _persist_jobs()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.0)


def _refresh_process_only_jobs() -> None:
    """If stored PID died, re-scan before declaring the upload finished."""
    with _lock:
        proc_jobs = [
            (jid, j.get("aws_pid"), j.get("dest"), j.get("batch"))
            for jid, j in _jobs.items()
            if j.get("status") == "running" and j.get("aws_pid") and not j.get("log_path")
        ]
    for job_id, pid, dest, batch in proc_jobs:
        if pid is None:
            continue
        if _pid_alive(int(pid)):
            continue
        # PID gone — maybe aws respawned under a new PID; discover before closing.
        _discover_live_aws_processes()
        with _lock:
            job = _jobs.get(job_id)
            if not job or job.get("status") != "running":
                continue
            # Still same job with dead pid and no replacement attached?
            if job.get("aws_pid") == pid and not _pid_alive(int(pid)):
                # Another running tracker for same dest/batch means we're fine.
                covered = any(
                    other.get("status") == "running"
                    and other is not job
                    and (
                        (dest and other.get("dest") == dest)
                        or (batch and other.get("batch") == batch)
                    )
                    for other in _jobs.values()
                )
                if covered:
                    job["status"] = "completed"
                    job["message"] = "Tracked by another live CMD upload"
                else:
                    # Keep as checking for a bit — sync may still be running under new PID.
                    job["status"] = "checking"
                    job["message"] = "aws PID changed — rechecking live sync…"
                    job["aws_pid"] = None
        _persist_jobs()


def _poll_all_jobs() -> None:
    with _lock:
        jobs = [dict(j) for j in _jobs.values() if j.get("status") == "running"]
    dirty = False
    for snapshot in jobs:
        if _ingest_log_progress(snapshot["id"]):
            dirty = True
    if dirty:
        _persist_jobs()


def _poll_s3_progress_for_jobs() -> None:
    """For CMD uploads (especially without logs), compare S3 size vs local folder size."""
    now = time.time()
    with _lock:
        targets = []
        for jid, job in _jobs.items():
            if job.get("status") != "running":
                continue
            dest = str(job.get("dest") or "")
            # Enrich from stored cmdline if needed.
            if not dest and job.get("log"):
                src, parsed_dest, batch = _parse_sync_cmdline(str(job["log"][0]))
                if parsed_dest:
                    job["dest"] = parsed_dest
                    dest = parsed_dest
                if src and not job.get("sources"):
                    job["sources"] = [src]
                if batch and (
                    not job.get("batch")
                    or "s3:" in str(job.get("batch"))
                    or str(job.get("batch")).startswith("pid-")
                ):
                    job["batch"] = batch
            sources = list(job.get("sources") or [])
            if not int(job.get("bytes_total") or 0) and sources:
                job["_need_total"] = sources[0]
            if not dest.startswith("s3://"):
                continue
            log_path = str(job.get("log_path") or "")
            if job.get("using_completed_meter"):
                continue
            log_quiet = (not log_path) or int(job.get("bytes_done") or 0) == 0
            if not log_quiet and not job.get("progress_via_s3") and not job.get("aws_pid"):
                continue
            last = float(job.get("last_s3_poll") or 0)
            if now - last < 12:
                continue
            job["last_s3_poll"] = now
            targets.append(
                (
                    jid,
                    dest,
                    int(job.get("bytes_total") or 0),
                    sources,
                    float(job.get("last_s3_bytes") or 0),
                    float(job.get("last_s3_poll_at") or job.get("started_at") or now),
                    str(job.get("_need_total") or ""),
                    dict(job.get("key_map") or {}),
                )
            )
            job.pop("_need_total", None)

    for job_id, dest, bytes_total, sources, prev_bytes, prev_at, need_total, key_map in targets:
        if bytes_total <= 0:
            root = need_total or (sources[0] if sources else "")
            if root:
                bytes_total = _dir_bytes(Path(root))
                with _lock:
                    if job_id in _jobs and bytes_total:
                        _jobs[job_id]["bytes_total"] = bytes_total
        dest_names = {str(v) for v in key_map.values()} if key_map else set()
        if dest_names:
            summary = _s3_bytes_for_names(dest, dest_names)
        else:
            summary = _s3_prefix_summary(dest)
        if summary is None:
            with _lock:
                job = _jobs.get(job_id)
                if job and job.get("status") == "running":
                    job["message"] = f"Uploading — querying S3 size for {dest}…"
            continue
        s3_bytes, s3_objects = summary
        elapsed = max(0.1, now - prev_at)
        delta = max(0, s3_bytes - prev_bytes)
        speed = (delta / (1024 * 1024)) / elapsed if delta > 0 else 0.0
        with _lock:
            job = _jobs.get(job_id)
            if not job or job.get("status") != "running":
                continue
            if job.get("using_completed_meter"):
                continue
            if bytes_total and not job.get("bytes_total"):
                job["bytes_total"] = bytes_total
            total = int(job.get("bytes_total") or bytes_total or 0)
            listed = min(total, s3_bytes) if total else s3_bytes
            prev_done = int(job.get("bytes_done") or 0)
            # Incomplete S3 listings must not drop the bar (12 GB → 15 GB → 12 GB).
            job["bytes_done"] = max(prev_done, listed)
            job["files_done"] = max(int(job.get("files_done") or 0), s3_objects)
            job["last_s3_bytes"] = s3_bytes
            job["last_s3_poll_at"] = now
            job["progress_via_s3"] = True
            if speed > 0:
                job["speed_mbps"] = speed
            elif job["bytes_done"] > 0:
                since = max(0.1, now - float(job.get("started_at") or now))
                job["speed_mbps"] = (job["bytes_done"] / (1024 * 1024)) / since
            remaining = max(0, total - int(job["bytes_done"]))
            mib_s = float(job.get("speed_mbps") or 0)
            if mib_s > 0 and remaining > 0:
                job["eta_seconds"] = int(remaining / (mib_s * 1024 * 1024))
            elif total and job["bytes_done"] >= total:
                job["eta_seconds"] = 0
            pct = int((job["bytes_done"] / total) * 100) if total else 0
            job["message"] = (
                f"Batch on S3: {pct}% · {job['bytes_done']}/{total or '?'} bytes "
                f"({s3_objects} objects). CMD may also show mid-file Completed X/Y."
            )
        _persist_jobs()


def _ingest_log_progress(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job.get("status") != "running":
            return False
        log_path = Path(str(job.get("log_path") or ""))
        offset = int(job.get("log_offset") or 0)
        started = float(job.get("started_at") or time.time())
        using_completed = bool(job.get("using_completed_meter"))
        transferred = int(job.get("transferred") or job.get("bytes_done") or 0)
        files_done = int(job.get("files_done") or 0)
        sources = [Path(p) for p in (job.get("sources") or []) if p]

    if not log_path.is_file():
        return False

    try:
        data = log_path.read_bytes()
    except OSError:
        return False
    if offset > len(data):
        offset = 0
    chunk = data[offset:].decode("utf-8", errors="replace")
    new_offset = len(data)
    if not chunk and not _log_has_exit(log_path):
        return False

    changed = False
    src_hint = sources[0] if sources else None
    for line in chunk.splitlines():
        line = line.rstrip()
        if not line:
            continue
        changed = True
        with _lock:
            job = _jobs.get(job_id)
            if not job:
                return False
            job["log"] = (job.get("log") or [])[-100:] + [line]
            if line.startswith(EXIT_MARKER):
                raw_code = line.split(":", 1)[1].strip() if ":" in line else "1"
                if raw_code.lower() == "cancelled" or job.get("cancel_requested") or job.get("status") in {
                    "cancelled",
                    "cancelling",
                }:
                    job["status"] = "cancelled"
                    job["message"] = (
                        "Cancelled — S3 may have a partial upload; click Retry to resume missing files"
                    )
                    job["log_offset"] = new_offset
                    job["speed_mbps"] = 0.0
                    job["pending_resync"] = False
                    return True
                try:
                    code = int(raw_code or "1")
                except ValueError:
                    code = 1
                if code == 0:
                    job["status"] = "completed"
                    job["bytes_done"] = job.get("bytes_total") or job.get("bytes_done") or 0
                    job["message"] = f"Uploaded to {job.get('dest') or 'S3'} — verifying sizes…"
                    job["eta_seconds"] = 0
                    job["log_offset"] = new_offset
                    need_resync = bool(job.get("pending_resync"))
                    resync_args = {
                        "s3_uri": str(job.get("pending_resync_s3_uri") or job.get("s3_uri") or ""),
                        "batch_name": str(job.get("batch") or ""),
                        "ssd1": str(job.get("pending_resync_ssd1") or ""),
                        "ssd2": str(job.get("pending_resync_ssd2") or ""),
                        "card_id": job.get("pending_resync_card_id"),
                    }
                    job["pending_resync"] = False
                    if need_resync and resync_args["s3_uri"] and resync_args["batch_name"]:
                        # Do not verify/delete this folder until the follow-up sync
                        # finishes (new job has auto_delete=True).
                        job["followup_resync"] = True
                        job["message"] = (
                            f"Uploaded to {job.get('dest') or 'S3'} — "
                            "starting follow-up sync for files added mid-upload"
                        )
                        threading.Thread(
                            target=_run_pending_resync,
                            kwargs=resync_args,
                            daemon=True,
                            name=f"aws-resync-{job_id[-12:]}",
                        ).start()
                    else:
                        threading.Thread(
                            target=_auto_verify_job,
                            args=(job_id,),
                            daemon=True,
                            name=f"aws-verify-{job_id[-12:]}",
                        ).start()
                else:
                    job["status"] = "error"
                    job["message"] = (
                        f"Sync failed (exit {code}) — click Retry (resume-safe)"
                    )
                    job["log_offset"] = new_offset
                job["speed_mbps"] = 0.0
                return True
            job["message"] = line[:220]

            speed = _parse_speed(line)
            if speed is not None:
                job["speed_mbps"] = speed

            done = _parse_completed_bytes(line)
            total_from_cmd = _parse_completed_total(line)
            if done is not None:
                using_completed = True
                transferred = max(transferred, done)
                job["using_completed_meter"] = True
                job["transferred"] = transferred
                # Match CMD Completed X/Y — don't cap against full local batch size
                job["bytes_done"] = done
                if total_from_cmd and total_from_cmd > 0:
                    job["bytes_total"] = total_from_cmd
                    job["cmd_total"] = total_from_cmd

            remain = _FILES_REMAINING_RE.search(line)
            if remain:
                job["files_remaining"] = int(remain.group(1))

            uploaded = _parse_upload_rel(line)
            if uploaded:
                files_done += 1
                job["files_done"] = files_done
                if not using_completed and src_hint is not None:
                    size = _resolve_upload_size(src_hint, uploaded)
                    if size <= 0:
                        for src in sources:
                            size = _resolve_upload_size(src, uploaded)
                            if size > 0:
                                break
                    if size > 0:
                        transferred += size
                        total = job.get("bytes_total") or 0
                        job["bytes_done"] = min(total, transferred) if total else transferred
                        job["transferred"] = transferred

            elapsed = max(0.1, time.time() - started)
            if job["bytes_done"] > 0 and float(job.get("speed_mbps") or 0) <= 0:
                job["speed_mbps"] = (job["bytes_done"] / (1024 * 1024)) / elapsed
            remaining = max(0, (job.get("bytes_total") or 0) - job["bytes_done"])
            mib_s = float(job.get("speed_mbps") or 0)
            if mib_s > 0 and remaining > 0:
                job["eta_seconds"] = int(remaining / (mib_s * 1024 * 1024))
            elif remaining <= 0 and (job.get("bytes_total") or 0) > 0:
                job["eta_seconds"] = 0
            job["log_offset"] = new_offset

    if changed:
        with _lock:
            if job_id in _jobs:
                _jobs[job_id]["log_offset"] = new_offset
                _jobs[job_id]["using_completed_meter"] = using_completed
                _jobs[job_id]["transferred"] = transferred
                _jobs[job_id]["files_done"] = files_done
    return changed


def _pid_alive(pid: int) -> bool:
    try:
        ps = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty Id",
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return bool(ps.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _resolve_upload_size(src_root: Path, rel: str) -> int:
    cleaned = rel.strip().strip('"').replace("/", os.sep).replace("\\", os.sep)
    candidates = [
        src_root / cleaned,
        Path(cleaned),
        src_root / Path(cleaned).name,
    ]
    if cleaned.startswith("." + os.sep):
        candidates.insert(0, src_root / cleaned[2:])
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.stat().st_size
        except OSError:
            continue
    return 0


def _parse_upload_rel(line: str) -> str | None:
    text = line.strip()
    match = _UPLOAD_RE.search(text)
    if match:
        return match.group(1).strip()
    match = _S5CMD_CP_RE.search(text)
    if match:
        return match.group(1).strip().strip('"')
    return None


def _to_bytes(value: float, unit: str) -> int:
    unit = unit.upper()
    if unit in {"B"}:
        return int(value)
    if unit in {"KB", "KIB"}:
        return int(value * 1024)
    if unit in {"MB", "MIB"}:
        return int(value * 1024 * 1024)
    if unit in {"GB", "GIB"}:
        return int(value * 1024 * 1024 * 1024)
    return int(value)


def _parse_speed(line: str) -> float | None:
    match = _SPEED_RE.search(line)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    if unit.startswith("G"):
        return value * 1024
    return value


def _parse_completed_bytes(line: str) -> int | None:
    match = _COMPLETED_RE.search(line)
    if not match:
        return None
    return _to_bytes(float(match.group(1)), match.group(2))


def _parse_completed_total(line: str) -> int | None:
    match = _COMPLETED_RE.search(line)
    if not match or not match.group(3) or not match.group(4):
        return None
    return _to_bytes(float(match.group(3)), match.group(4))


def list_external_jobs() -> list[dict]:
    return list_jobs()

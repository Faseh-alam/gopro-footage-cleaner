"""AWS S3 sync using the AWS CLI (credentials from `aws configure`).

Uploads run in an **external Command Prompt / Terminal** so a server restart
does **not** stop them. Output is tee'd to a log file under ``state/aws_logs/``.
The offloader watches those logs and shows size / speed / ETA in the UI.
On startup it re-attaches to any still-running uploads (open CMD + log).
``aws s3 sync`` is resume-safe if you ever need to start the same batch again.
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

from .config import BATCHES_SUBDIR, STATE_DIR, ensure_dirs

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_monitor_started = False
JOBS_FILE = STATE_DIR / "aws_jobs.json"
LOG_DIR = STATE_DIR / "aws_logs"
EXIT_MARKER = "OFFLOADER_EXIT:"

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
_BATCH_IN_PATH_RE = re.compile(r"[\\/]Batches[\\/]([^\\\"'\s/]+)", re.IGNORECASE)
_SYNC_ARGS_RE = re.compile(
    r"s3\s+sync\s+(?:\"([^\"]+)\"|(\S+))\s+(?:\"(s3://[^\"]+)\"|(s3://\S+))",
    re.IGNORECASE,
)
_TOTAL_SIZE_RE = re.compile(r"Total Size:\s*(\d+)", re.IGNORECASE)
_TOTAL_OBJECTS_RE = re.compile(r"Total Objects:\s*(\d+)", re.IGNORECASE)


def aws_cli_available() -> bool:
    return shutil.which("aws") is not None


def test_aws_connection(s3_uri: str) -> dict:
    """Upload a tiny empty file via AWS CLI credentials (`aws configure`)."""
    if not aws_cli_available():
        raise RuntimeError("AWS CLI not found. Install AWS CLI v2, then run `aws configure`.")

    base = normalize_s3_uri(s3_uri)
    key = f"{base}_offloader_connection_test.txt"

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "offloader_connection_test.txt"
        local.write_text("", encoding="utf-8")
        put = subprocess.run(
            ["aws", "s3", "cp", str(local), key],
            capture_output=True,
            text=True,
        )
        if put.returncode != 0:
            detail = (put.stderr or put.stdout or "aws s3 cp failed").strip()
            raise RuntimeError(detail)

        delete = subprocess.run(
            ["aws", "s3", "rm", key],
            capture_output=True,
            text=True,
        )
        cleaned = delete.returncode == 0

    return {
        "ok": True,
        "message": (
            f"AWS OK — uploaded and verified write to {key}"
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
    base = normalize_s3_uri(s3_uri)
    return f"{base}{batch_name.strip().strip('/')}/"


def list_local_batch_roots(ssd1: str, ssd2: str, batch_name: str) -> list[Path]:
    roots = []
    for ssd in (ssd1, ssd2):
        if not ssd:
            continue
        root = Path(ssd) / BATCHES_SUBDIR / batch_name.strip()
        if root.is_dir():
            roots.append(root)
    return roots


def _dir_bytes(root: Path) -> int:
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


def start_batch_upload(
    *,
    s3_uri: str,
    batch_name: str,
    ssd1: str,
    ssd2: str,
    card_id: str | None = None,
    external_window: bool = True,
    show_console: bool | None = None,
) -> dict:
    """Start aws s3 sync in an external console (survives server restart)."""
    del show_console  # always external + logged
    if not aws_cli_available():
        raise RuntimeError("AWS CLI not found. Install AWS CLI v2 and run `aws configure`.")

    prefix = batch_s3_prefix(s3_uri, batch_name)
    roots = list_local_batch_roots(ssd1, ssd2, batch_name)
    if not roots:
        raise RuntimeError(f"No local batch folder found for {batch_name} on the selected SSDs")

    sources: list[Path] = []
    if card_id:
        for root in roots:
            card_path = root / card_id.upper()
            if card_path.is_dir():
                sources.append(card_path)
        if not sources:
            raise RuntimeError(f"Card folder {card_id} not found under batch {batch_name}")
        dest = f"{prefix}{card_id.upper()}/"
    else:
        sources = roots
        dest = prefix

    total_bytes = sum(_dir_bytes(src) for src in sources)
    stamp = int(time.time())
    label = f"{batch_name}-{card_id or 'ALL'}-{stamp}"
    job_id = f"aws:{label}"
    safe = re.sub(r"[^\w.-]+", "_", label)

    ensure_dirs()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{safe}.log"
    script_path = LOG_DIR / f"{safe}{'.bat' if platform.system() == 'Windows' else '.sh'}"

    header = (
        f"AWS S3 upload  {batch_name}"
        + (f" / {card_id}" if card_id else "")
        + f"\nDestination: {dest}\n"
        f"Local size: {total_bytes} bytes\n"
        "This CMD window keeps uploading even if you restart the offloader.\n"
        "============================================\n"
    )
    log_path.write_text(header, encoding="utf-8")

    _write_external_script(script_path, sources=sources, dest=dest, log_path=log_path, title=f"AWS — {batch_name}")
    _launch_external_script(script_path, title=f"AWS upload — {batch_name}" + (f" / {card_id}" if card_id else ""))

    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "running",
            "batch": batch_name,
            "card_id": card_id,
            "dest": dest,
            "bytes_done": 0,
            "bytes_total": total_bytes,
            "files_done": 0,
            "speed_mbps": 0.0,
            "eta_seconds": None,
            "message": f"CMD upload started → {dest} (survives server restart)",
            "log": [f"Local size {total_bytes} bytes", f"Script {script_path}"],
            "started_at": time.time(),
            "external": True,
            "console": True,
            "log_path": str(log_path),
            "script": str(script_path),
            "sources": [str(s) for s in sources],
            "log_offset": 0,
            "using_completed_meter": False,
            "transferred": 0,
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
) -> None:
    """Write a console script that runs sync and tees output into log_path."""
    if platform.system() == "Windows":
        # PowerShell Tee-Object → console + log; EXIT marker for the UI monitor.
        lines = [
            "@echo off",
            "setlocal",
            f"title {title}",
            "echo ============================================",
            f"echo   {title}",
            f"echo   Destination: {dest}",
            "echo   Progress also appears in the offloader web UI.",
            "echo   Closing this window STOPS the upload.",
            "echo   Restarting the offloader does NOT stop this window.",
            "echo ============================================",
            "echo.",
        ]
        log_ps = str(log_path).replace("'", "''")
        for src in sources:
            src_ps = str(src).replace("'", "''")
            dest_ps = dest.replace("'", "''")
            lines.append(f'echo Syncing "{src}"')
            # Tee live output to the log file the server watches.
            lines.append(
                "powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
                f"\"& {{ aws s3 sync '{src_ps}' '{dest_ps}' 2>&1 | "
                f"Tee-Object -FilePath '{log_ps}' -Append ; "
                f"if ($LASTEXITCODE -ne $null) {{ exit $LASTEXITCODE }} else {{ exit 0 }} }}\""
            )
            lines.append("set SYNC_ERR=%ERRORLEVEL%")
            lines.append("if %SYNC_ERR% neq 0 (")
            lines.append(f"  echo {EXIT_MARKER}%SYNC_ERR%>> \"{log_path}\"")
            lines.append("  echo.")
            lines.append("  echo ERROR: aws s3 sync failed. Fix network/credentials and re-run.")
            lines.append("  pause")
            lines.append("  exit /b %SYNC_ERR%")
            lines.append(")")
            lines.append("echo.")
        lines.append(f"echo {EXIT_MARKER}0>> \"{log_path}\"")
        lines.append("echo ============================================")
        lines.append("echo   Upload finished OK")
        lines.append("echo ============================================")
        lines.append("pause")
        script_path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    else:
        lines = [
            "#!/bin/bash",
            "set -e",
            f'echo "============================================"',
            f'echo "  {title}"',
            f'echo "  Destination: {dest}"',
            'echo "  Progress also appears in the offloader web UI."',
            'echo "============================================"',
            "echo",
        ]
        for src in sources:
            lines.append(f'echo "Syncing {src}"')
            lines.append(
                f'aws s3 sync "{src}" "{dest}" 2>&1 | tee -a "{log_path}"'
            )
            lines.append("ec=${PIPESTATUS[0]}")
            lines.append(f'echo "{EXIT_MARKER}${{ec}}" >> "{log_path}"')
            lines.append('if [[ "$ec" -ne 0 ]]; then echo "ERROR: sync failed"; read -r; exit "$ec"; fi')
            lines.append("echo")
        lines.append(f'echo "{EXIT_MARKER}0" >> "{log_path}"')
        lines.extend(
            [
                'echo "============================================"',
                'echo "  Upload finished OK"',
                'echo "============================================"',
                'read -r -p "Press Enter to close…" _',
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
                            else:
                                job["status"] = "error"
                                job["message"] = f"aws s3 sync failed (exit {code})"
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
                "No live aws s3 sync found — if CMD is still uploading, wait a few seconds "
                "or click Upload to resume (sync skips files already on S3)"
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
    """Return (local_source, s3_dest, batch_name) from an aws s3 sync command line."""
    match = _SYNC_ARGS_RE.search(cmd or "")
    if not match:
        return None, None, None
    src = (match.group(1) or match.group(2) or "").strip().rstrip("\\/")
    dest = (match.group(3) or match.group(4) or "").strip()
    if dest and not dest.endswith("/"):
        dest += "/"
    batch = None
    if src:
        bm = _BATCH_IN_PATH_RE.search(src)
        if bm:
            batch = bm.group(1).strip()
    if not batch and dest:
        parts = [p for p in dest.rstrip("/").split("/") if p]
        if parts:
            batch = parts[-1]
    return src or None, dest or None, batch


def _s3_prefix_summary(dest: str) -> tuple[int, int] | None:
    """Return (total_bytes, total_objects) already on S3 under dest, or None."""
    if not dest.startswith("s3://"):
        return None
    try:
        result = subprocess.run(
            ["aws", "s3", "ls", dest, "--recursive", "--summarize"],
            capture_output=True,
            text=True,
            timeout=180,
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


def _discover_live_aws_processes() -> None:
    """Detect aws s3 sync still running in CMD (including pre-log older uploads)."""
    if platform.system() != "Windows":
        return
    try:
        ps = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"name='aws.exe'\" | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
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
        if "s3" not in cmd.lower() or "sync" not in cmd.lower():
            continue
        src, dest, batch = _parse_sync_cmdline(cmd)
        # Never walk multi-TB trees here — that blocked server startup for minutes.
        bytes_total = 0

        with _lock:
            # Prefer an existing job for same batch / dest / pid (revive interrupted).
            existing_id = None
            for jid, j in _jobs.items():
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
            if existing_id:
                job = _jobs[existing_id]
                job["status"] = "running"
                job["aws_pid"] = pid
                job["console"] = True
                job["external"] = True
                job["progress_via_s3"] = True
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
                    f"Live CMD upload (PID {pid}"
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
                if src:
                    job["sources"] = [src]
                if dest:
                    job["dest"] = dest
                if batch:
                    job["batch"] = batch
                if bytes_total:
                    job["bytes_total"] = bytes_total
                job["message"] = (
                    f"Live CMD upload (PID {pid}"
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
                    f"Live CMD upload (PID {pid}"
                    + (f", {batch}" if batch else "")
                    + ") — measuring progress via S3 (safe to leave CMD open)"
                ),
                "log": [cmd[:400]],
                "started_at": time.time(),
                "external": True,
                "console": True,
                "aws_pid": pid,
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
            row = {k: v for k, v in job.items() if k not in {"sources"}}
            row["log"] = list(row.get("log") or [])[-40:]
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
                )
            )
            job.pop("_need_total", None)

    for job_id, dest, bytes_total, sources, prev_bytes, prev_at, need_total in targets:
        if bytes_total <= 0:
            root = need_total or (sources[0] if sources else "")
            if root:
                bytes_total = _dir_bytes(Path(root))
                with _lock:
                    if job_id in _jobs and bytes_total:
                        _jobs[job_id]["bytes_total"] = bytes_total
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
            job["bytes_done"] = min(total, s3_bytes) if total else s3_bytes
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
                try:
                    code = int(line.split(":", 1)[1].strip() or "1")
                except ValueError:
                    code = 1
                if code == 0:
                    job["status"] = "completed"
                    job["bytes_done"] = job.get("bytes_total") or job.get("bytes_done") or 0
                    job["message"] = f"Uploaded to {job.get('dest') or 'S3'}"
                    job["eta_seconds"] = 0
                else:
                    job["status"] = "error"
                    job["message"] = f"aws s3 sync failed (exit {code})"
                job["speed_mbps"] = 0.0
                job["log_offset"] = new_offset
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
    match = _UPLOAD_RE.search(line.strip())
    if not match:
        return None
    return match.group(1).strip()


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

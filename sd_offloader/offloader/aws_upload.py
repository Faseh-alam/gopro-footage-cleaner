"""AWS S3 sync using the AWS CLI (credentials from `aws configure`).

Large batch uploads open a **local Command Prompt / Terminal window** so progress
keeps running even if the browser is closed. ``aws s3 sync`` is resume-safe —
re-run the same script and it skips files already on S3.
"""

from __future__ import annotations

import json
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
EXTERNAL_JOBS_FILE = STATE_DIR / "aws_external_jobs.json"


def aws_cli_available() -> bool:
    return shutil.which("aws") is not None


def test_aws_connection(s3_uri: str) -> dict:
    """Upload a tiny empty file via AWS CLI credentials (`aws configure`).

    Does not read/write app secret files — only uses the S3 URI from the UI and
    whatever profile ``aws configure`` already set up.
    """
    if not aws_cli_available():
        raise RuntimeError("AWS CLI not found. Install AWS CLI v2, then run `aws configure`.")

    base = normalize_s3_uri(s3_uri)
    # Marker under the configured prefix so it lands where footage would go
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

        # Best-effort cleanup so the bucket isn't littered
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


def start_batch_upload(
    *,
    s3_uri: str,
    batch_name: str,
    ssd1: str,
    ssd2: str,
    card_id: str | None = None,
    external_window: bool = True,
) -> dict:
    """Start an S3 sync. Prefer a visible local console for long TB-scale uploads."""
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

    if external_window:
        return start_external_sync(
            batch_name=batch_name,
            sources=sources,
            dest=dest,
            card_id=card_id,
        )

    job_id = f"{batch_name}:{card_id or 'ALL'}:{int(time.time())}"
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "running",
            "batch": batch_name,
            "card_id": card_id,
            "dest": dest,
            "bytes_done": 0,
            "bytes_total": 0,
            "speed_mbps": 0.0,
            "eta_seconds": None,
            "message": "Starting aws s3 sync…",
            "log": [],
            "started_at": time.time(),
            "external": False,
        }

    thread = threading.Thread(
        target=_run_sync_job,
        args=(job_id, sources, dest),
        daemon=True,
        name=f"aws-{job_id}",
    )
    thread.start()
    return get_job(job_id) or {"id": job_id, "status": "running"}


def start_external_sync(
    *,
    batch_name: str,
    sources: list[Path],
    dest: str,
    card_id: str | None = None,
) -> dict:
    """Write a sync script and open it in a new Command Prompt / Terminal window."""
    ensure_dirs()
    stamp = int(time.time())
    label = f"{batch_name}-{card_id or 'ALL'}-{stamp}"
    safe = re.sub(r"[^\w.-]+", "_", label)
    system = platform.system()

    if system == "Windows":
        script = STATE_DIR / f"aws_upload_{safe}.bat"
        lines = [
            "@echo off",
            "setlocal",
            f"title AWS upload — {batch_name}" + (f" / {card_id}" if card_id else ""),
            "echo ============================================",
            f"echo   AWS S3 upload  {batch_name}"
            + (f" / {card_id}" if card_id else ""),
            f"echo   Destination: {dest}",
            "echo   aws s3 sync is resume-safe — re-run this window if interrupted.",
            "echo ============================================",
            "echo.",
        ]
        for src in sources:
            lines.append(f'echo Syncing "{src}"')
            lines.append(f'echo   -^> {dest}')
            # /Y not needed; sync skips existing matching files
            lines.append(f'aws s3 sync "{src}" "{dest}"')
            lines.append("if errorlevel 1 (")
            lines.append("  echo.")
            lines.append("  echo ERROR: aws s3 sync failed. Fix network/credentials and re-run this script.")
            lines.append("  pause")
            lines.append("  exit /b 1")
            lines.append(")")
            lines.append("echo.")
        lines.extend(
            [
                "echo ============================================",
                "echo   Upload finished OK",
                "echo ============================================",
                "pause",
            ]
        )
        script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        # New visible console that survives browser close
        subprocess.Popen(
            ["cmd.exe", "/c", "start", f"AWS upload — {batch_name}", "cmd.exe", "/k", str(script)],
            cwd=str(STATE_DIR),
        )
    else:
        script = STATE_DIR / f"aws_upload_{safe}.sh"
        lines = [
            "#!/bin/bash",
            "set -e",
            f'echo "============================================"',
            f'echo "  AWS S3 upload  {batch_name}'
            + (f" / {card_id}" if card_id else "")
            + '"',
            f'echo "  Destination: {dest}"',
            'echo "  aws s3 sync is resume-safe — re-run if interrupted."',
            'echo "============================================"',
            "echo",
        ]
        for src in sources:
            lines.append(f'echo "Syncing {src}"')
            lines.append(f'aws s3 sync "{src}" "{dest}"')
            lines.append("echo")
        lines.extend(
            [
                'echo "============================================"',
                'echo "  Upload finished OK"',
                'echo "============================================"',
                'read -r -p "Press Enter to close…" _',
            ]
        )
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script.chmod(0o755)
        if system == "Darwin":
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    f'tell application "Terminal" to do script "bash \\"{script}\\""',
                ]
            )
        else:
            # Linux: try a common terminal
            for term in ("x-terminal-emulator", "gnome-terminal", "xterm"):
                if shutil.which(term):
                    subprocess.Popen([term, "-e", f"bash {script}"])
                    break
            else:
                raise RuntimeError("No terminal found to show AWS progress")

    job_id = f"external:{label}"
    job = {
        "id": job_id,
        "status": "external",
        "batch": batch_name,
        "card_id": card_id,
        "dest": dest,
        "bytes_done": 0,
        "bytes_total": 0,
        "speed_mbps": 0.0,
        "eta_seconds": None,
        "message": f"Opened local console — syncing to {dest} (resume-safe)",
        "log": [f"Script: {script}"],
        "started_at": time.time(),
        "external": True,
        "script": str(script),
    }
    with _lock:
        _jobs[job_id] = job
    _remember_external_job(job)
    return dict(job)


def _remember_external_job(job: dict) -> None:
    ensure_dirs()
    rows: list[dict] = []
    if EXTERNAL_JOBS_FILE.exists():
        try:
            rows = json.loads(EXTERNAL_JOBS_FILE.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
        except (json.JSONDecodeError, OSError):
            rows = []
    rows.insert(0, job)
    rows = rows[:40]
    try:
        EXTERNAL_JOBS_FILE.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except OSError:
        pass


def list_external_jobs() -> list[dict]:
    if not EXTERNAL_JOBS_FILE.exists():
        return []
    try:
        rows = json.loads(EXTERNAL_JOBS_FILE.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def list_jobs() -> list[dict]:
    with _lock:
        live = [dict(j) for j in sorted(_jobs.values(), key=lambda x: x.get("started_at", 0), reverse=True)]
    # Merge recent external jobs from disk (survive app restart in the UI list)
    seen = {j["id"] for j in live}
    for row in list_external_jobs():
        jid = row.get("id")
        if jid and jid not in seen:
            live.append(row)
            seen.add(jid)
    live.sort(key=lambda x: x.get("started_at", 0), reverse=True)
    return live


def _run_sync_job(job_id: str, sources: list[Path], dest: str) -> None:
    total_bytes = 0
    for src in sources:
        for path in src.rglob("*"):
            if path.is_file():
                try:
                    total_bytes += path.stat().st_size
                except OSError:
                    pass

    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["bytes_total"] = total_bytes

    transferred = 0
    started = time.time()

    for src in sources:
        cmd = ["aws", "s3", "sync", str(src), dest]
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            if not line:
                continue
            with _lock:
                job = _jobs.get(job_id)
                if not job:
                    continue
                job["log"] = (job.get("log") or [])[-80:] + [line]
                job["message"] = line[:200]
                speed = _parse_speed(line)
                if speed is not None:
                    job["speed_mbps"] = speed
                done = _parse_completed_bytes(line)
                if done is not None:
                    transferred = max(transferred, done)
                    job["bytes_done"] = min(total_bytes, transferred)
                    elapsed = max(0.1, time.time() - started)
                    if job["speed_mbps"] <= 0 and transferred > 0:
                        job["speed_mbps"] = (transferred / (1024 * 1024)) / elapsed
                    remaining = max(0, total_bytes - job["bytes_done"])
                    mib_s = job["speed_mbps"]
                    if mib_s > 0:
                        job["eta_seconds"] = int(remaining / (mib_s * 1024 * 1024))

        code = process.wait()
        if code != 0:
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["status"] = "error"
                    _jobs[job_id]["message"] = f"aws s3 sync failed (exit {code})"
            return

    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["bytes_done"] = total_bytes
            _jobs[job_id]["message"] = f"Uploaded to {dest}"
            _jobs[job_id]["eta_seconds"] = 0


_SPEED_RE = re.compile(r"([\d.]+)\s*(MiB|MB|GiB|GB)/s", re.IGNORECASE)
_COMPLETED_RE = re.compile(r"Completed\s+([\d.]+)\s*(MiB|MB|GiB|GB|KiB|KB|B)", re.IGNORECASE)


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
    return value  # MiB/s


def _parse_completed_bytes(line: str) -> int | None:
    match = _COMPLETED_RE.search(line)
    if not match:
        return None
    return _to_bytes(float(match.group(1)), match.group(2))

"""Running hours ledger toward 1000-hour batches (batch-1, batch-2, …).

Soft roll: a card already copying keeps its assigned batch; after that card
finishes and the batch crosses the target, *new* cards go to the next batch.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from .config import BATCHES_SUBDIR, STATE_DIR, ensure_dirs, load_config, save_config

_lock = threading.RLock()
LEDGER_JSON = STATE_DIR / "hours_ledger.json"
LEDGER_TXT = STATE_DIR / "hours_ledger.txt"
_BATCH_RE = re.compile(r"^batch-(\d+)$", re.IGNORECASE)

DEFAULT_TARGET_HOURS = 1000.0


def _target_hours() -> float:
    try:
        n = float(load_config().get("batch_hours_target") or DEFAULT_TARGET_HOURS)
    except (TypeError, ValueError):
        n = DEFAULT_TARGET_HOURS
    return max(1.0, n)


def _empty_ledger() -> dict:
    return {
        "version": 1,
        "target_hours": _target_hours(),
        "active_batch": "batch-1",
        "batches": {
            "batch-1": {
                "status": "open",
                "hours": 0.0,
                "seconds": 0.0,
                "cards": [],
            }
        },
        "events": [],
    }


def _load() -> dict:
    ensure_dirs()
    if not LEDGER_JSON.exists():
        data = _empty_ledger()
        _save(data)
        return data
    try:
        data = json.loads(LEDGER_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = _empty_ledger()
    if not isinstance(data, dict):
        data = _empty_ledger()
    data.setdefault("version", 1)
    data.setdefault("target_hours", _target_hours())
    data.setdefault("active_batch", "batch-1")
    data.setdefault("batches", {})
    data.setdefault("events", [])
    if "batch-1" not in data["batches"]:
        data["batches"]["batch-1"] = {
            "status": "open",
            "hours": 0.0,
            "seconds": 0.0,
            "cards": [],
        }
    return data


def _save(data: dict) -> None:
    ensure_dirs()
    LEDGER_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _write_txt(data)


def _write_txt(data: dict) -> None:
    target = float(data.get("target_hours") or _target_hours())
    lines = [
        "GoPro offloader — batch hours ledger",
        f"Target per batch: {target:.0f} hours",
        f"Active batch: {data.get('active_batch')}",
        "",
    ]
    for name in sorted(data.get("batches") or {}, key=_batch_sort_key):
        row = data["batches"][name]
        hours = float(row.get("hours") or 0)
        status = row.get("status") or "open"
        lines.append(f"=== {name} ({status}) · {hours:.2f} / {target:.0f} h ===")
        for card in row.get("cards") or []:
            lines.append(
                f"  {card.get('recorded_at', '')}  {card.get('card_id')}  "
                f"{float(card.get('hours') or 0):.2f} h  → {card.get('dest', '')}"
            )
        lines.append("")
    lines.append("--- recent events ---")
    for ev in (data.get("events") or [])[-40:]:
        lines.append(f"  {ev}")
    LEDGER_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _batch_sort_key(name: str) -> tuple[int, str]:
    m = _BATCH_RE.match(name.strip())
    if m:
        return (int(m.group(1)), name.lower())
    return (10_000, name.lower())


def _next_batch_name(existing: dict[str, dict]) -> str:
    highest = 0
    for name in existing:
        m = _BATCH_RE.match(name.strip())
        if m:
            highest = max(highest, int(m.group(1)))
    return f"batch-{highest + 1}"


def _ensure_batch_row(data: dict, name: str) -> dict:
    batches = data.setdefault("batches", {})
    if name not in batches:
        batches[name] = {
            "status": "open",
            "hours": 0.0,
            "seconds": 0.0,
            "cards": [],
        }
    return batches[name]


def _mkdir_batch_on_ssds(batch: str, ssd1: str = "", ssd2: str = "") -> list[str]:
    created = []
    for ssd in (ssd1, ssd2):
        if not ssd:
            continue
        root = Path(ssd) / BATCHES_SUBDIR / batch
        root.mkdir(parents=True, exist_ok=True)
        created.append(str(root))
    return created


def _maybe_roll(data: dict, ssd1: str = "", ssd2: str = "") -> str | None:
    """If active batch is at/over target, mark full and open the next. Returns new name or None."""
    target = float(data.get("target_hours") or _target_hours())
    active = str(data.get("active_batch") or "batch-1")
    row = _ensure_batch_row(data, active)
    hours = float(row.get("hours") or 0)
    if hours < target:
        return None
    row["status"] = "full"
    nxt = _next_batch_name(data["batches"])
    _ensure_batch_row(data, nxt)
    data["batches"][nxt]["status"] = "open"
    data["active_batch"] = nxt
    _mkdir_batch_on_ssds(nxt, ssd1, ssd2)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"{stamp}  ROLLED {active} full at {hours:.2f}h → active {nxt}"
    data.setdefault("events", []).append(msg)
    save_config({"last_batch": nxt})
    return nxt


def ensure_active_batch(ssd1: str = "", ssd2: str = "") -> str:
    """Return open active batch-N, rolling if already at target. Creates folders on SSDs."""
    with _lock:
        data = _load()
        data["target_hours"] = _target_hours()
        rolled = _maybe_roll(data, ssd1, ssd2)
        active = str(data.get("active_batch") or "batch-1")
        _ensure_batch_row(data, active)
        data["batches"][active]["status"] = "open"
        _mkdir_batch_on_ssds(active, ssd1, ssd2)
        _save(data)
        if rolled:
            pass  # event already logged
        return active


def record_card(
    *,
    card_id: str,
    seconds: float,
    batch: str,
    dest: str,
    ssd: str = "",
    probed_ok: int = 0,
    probed_fail: int = 0,
    ssd1: str = "",
    ssd2: str = "",
) -> dict:
    """Append card hours into ``batch``. Soft-roll active when that batch hits target."""
    seconds = max(0.0, float(seconds))
    hours = seconds / 3600.0
    batch = (batch or "").strip() or "batch-1"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        data = _load()
        data["target_hours"] = _target_hours()
        row = _ensure_batch_row(data, batch)
        entry = {
            "card_id": card_id,
            "seconds": round(seconds, 3),
            "hours": round(hours, 4),
            "dest": dest,
            "ssd": ssd,
            "probed_ok": probed_ok,
            "probed_fail": probed_fail,
            "recorded_at": stamp,
        }
        row.setdefault("cards", []).append(entry)
        row["seconds"] = float(row.get("seconds") or 0) + seconds
        row["hours"] = float(row.get("seconds") or 0) / 3600.0
        data.setdefault("events", []).append(
            f"{stamp}  {card_id}  +{hours:.2f}h → {batch} (now {row['hours']:.2f}h)"
        )
        rolled_to = None
        # Soft roll: only change active for *future* cards when this batch (if active) is full.
        if str(data.get("active_batch")) == batch:
            rolled_to = _maybe_roll(data, ssd1 or ssd, ssd2)
        _save(data)
        summary = get_summary_unlocked(data)
        summary["recorded"] = entry
        summary["rolled_to"] = rolled_to
        return summary


def get_summary() -> dict:
    with _lock:
        return get_summary_unlocked(_load())


def get_summary_unlocked(data: dict) -> dict:
    target = float(data.get("target_hours") or _target_hours())
    active = str(data.get("active_batch") or "batch-1")
    batches_out = []
    grand = 0.0
    for name in sorted(data.get("batches") or {}, key=_batch_sort_key):
        row = data["batches"][name]
        hours = float(row.get("hours") or 0)
        grand += hours
        batches_out.append(
            {
                "name": name,
                "status": row.get("status") or "open",
                "hours": round(hours, 3),
                "seconds": round(float(row.get("seconds") or 0), 1),
                "cards": len(row.get("cards") or []),
                "remaining_hours": max(0.0, round(target - hours, 3)),
                "is_active": name == active,
            }
        )
    active_row = next((b for b in batches_out if b["name"] == active), None)
    return {
        "target_hours": target,
        "active_batch": active,
        "active_hours": active_row["hours"] if active_row else 0.0,
        "active_remaining_hours": active_row["remaining_hours"] if active_row else target,
        "grand_total_hours": round(grand, 3),
        "batches": batches_out,
        "ledger_json": str(LEDGER_JSON),
        "ledger_txt": str(LEDGER_TXT),
        "recent_events": list(data.get("events") or [])[-12:],
    }


def list_numbered_batches_on_ssds(ssd1: str = "", ssd2: str = "") -> list[dict]:
    """Each existing Batches/batch-N folder on each SSD (for per-drive upload)."""
    found: list[dict] = []
    for idx, ssd in enumerate((ssd1, ssd2), start=1):
        if not ssd:
            continue
        root = Path(ssd)
        batches_dir = root / BATCHES_SUBDIR
        if not batches_dir.is_dir():
            continue
        drive = _drive_label(root, idx)
        try:
            entries = sorted(batches_dir.iterdir(), key=lambda p: _batch_sort_key(p.name))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            # Only true batch-N folders for the hours workflow; still include other names for upload.
            has_files = False
            try:
                for child in entry.rglob("*"):
                    if child.is_file():
                        has_files = True
                        break
            except OSError:
                pass
            found.append(
                {
                    "batch": entry.name,
                    "ssd": str(root),
                    "path": str(entry),
                    "drive": drive,
                    "has_files": has_files,
                }
            )
    return found


def _drive_label(root: Path, index: int) -> str:
    s = str(root)
    # Windows drive letter
    if len(s) >= 2 and s[1] == ":":
        return s[:2].upper()
    return f"SSD{index}"

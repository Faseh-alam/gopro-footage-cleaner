"""Durable batch registry: CSV import, card matching, completion, reports.

Temporary CSV columns (one row per memory card):
  batch_name, factory, card_badge, device_type, device_id
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import annotation_store
from .volumes import list_sd_cards

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT_ROOT / "state" / "batches"
REQUIRED_COLUMNS = ("batch_name", "factory", "card_badge", "device_type", "device_id")

_lock = threading.RLock()


def ensure_state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(raw)
        temp = handle.name
    Path(temp).replace(path)


def _batch_path(batch_id: str) -> Path:
    return ensure_state_dir() / f"{batch_id}.json"


def _normalize_header(value: str) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )


def parse_batch_csv(text: str) -> dict:
    """Parse temporary batch CSV. Returns preview payload or raises ValueError."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    mapping: dict[str, str] = {}
    for field in reader.fieldnames:
        key = _normalize_header(field)
        aliases = {
            "batch": "batch_name",
            "batchname": "batch_name",
            "factory_name": "factory",
            "card": "card_badge",
            "card_id": "card_badge",
            "badge": "card_badge",
            "device": "device_type",
            "camera_type": "device_type",
            "camera_id": "device_id",
            "deviceid": "device_id",
        }
        canonical = aliases.get(key, key)
        if canonical in REQUIRED_COLUMNS:
            mapping[canonical] = field

    missing = [c for c in REQUIRED_COLUMNS if c not in mapping]
    if missing:
        raise ValueError(f"CSV missing columns: {', '.join(missing)}")

    rows: list[dict] = []
    batch_names: set[str] = set()
    badges: set[str] = set()
    for index, raw in enumerate(reader, start=2):
        row = {
            col: str(raw.get(mapping[col]) or "").strip()
            for col in REQUIRED_COLUMNS
        }
        if not any(row.values()):
            continue
        empty = [c for c, v in row.items() if not v]
        if empty:
            raise ValueError(f"Row {index} missing: {', '.join(empty)}")
        badge = row["card_badge"].upper()
        row["card_badge"] = badge
        if badge in badges:
            raise ValueError(f"Duplicate card_badge in CSV: {badge}")
        badges.add(badge)
        batch_names.add(row["batch_name"])
        rows.append(row)

    if not rows:
        raise ValueError("CSV has no data rows")
    if len(batch_names) != 1:
        raise ValueError(
            f"CSV must contain exactly one batch_name; found: {', '.join(sorted(batch_names))}"
        )

    batch_name = next(iter(batch_names))
    return {
        "batch_name": batch_name,
        "factory": rows[0]["factory"],
        "cards": rows,
        "card_count": len(rows),
        "device_types": sorted({r["device_type"] for r in rows}),
    }


def list_batches() -> list[dict]:
    ensure_state_dir()
    rows = []
    for path in sorted(STATE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        rows.append(_public_batch(data))
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return rows


def get_batch(batch_id: str) -> dict | None:
    path = _batch_path(batch_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _save(data: dict) -> dict:
    data["updated_at"] = _now_iso()
    _atomic_write(_batch_path(data["id"]), data)
    return data


def _public_batch(data: dict) -> dict:
    report = compute_report(data)
    return {
        "id": data.get("id"),
        "batch_name": data.get("batch_name"),
        "factory": data.get("factory"),
        "status": data.get("status"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "completed_at": data.get("completed_at"),
        "card_count": len(data.get("cards") or []),
        "cards_done": sum(1 for c in (data.get("cards") or []) if c.get("status") == "complete"),
        "report": report.get("totals"),
        "blocking": report.get("blocking") or [],
    }


def create_batch_from_csv(text: str) -> dict:
    preview = parse_batch_csv(text)
    batch_id = f"{annotation_store.safe_batch_id(preview['batch_name'])}-{uuid.uuid4().hex[:8]}"
    cards = []
    for row in preview["cards"]:
        cards.append(
            {
                "card_badge": row["card_badge"],
                "factory": row["factory"],
                "device_type": row["device_type"],
                "device_id": row["device_id"],
                "status": "expected",  # expected | bound | reviewing | complete
                "mount_path": "",
                "scan_path": "",
                "bound_at": None,
                "completed_at": None,
                "assets": [],  # filled when card is scanned
            }
        )
    data = {
        "id": batch_id,
        "batch_name": preview["batch_name"],
        "factory": preview["factory"],
        "status": "open",  # open | complete
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "completed_at": None,
        "csv_preview": {
            "card_count": preview["card_count"],
            "device_types": preview["device_types"],
        },
        "cards": cards,
    }
    with _lock:
        _save(data)
    return get_batch_detail(batch_id)


def get_batch_detail(batch_id: str) -> dict:
    data = get_batch(batch_id)
    if not data:
        raise FileNotFoundError(f"Batch not found: {batch_id}")
    report = compute_report(data)
    return {
        **_public_batch(data),
        "cards": data.get("cards") or [],
        "report": report,
    }


def match_detected_cards(batch_id: str) -> dict:
    """Match currently inserted SD cards to expected CSV badges."""
    data = get_batch(batch_id)
    if not data:
        raise FileNotFoundError(f"Batch not found: {batch_id}")
    if data.get("status") == "complete":
        raise ValueError("Batch is already complete")

    detected = list_sd_cards()
    by_badge = {str(c.get("id") or "").upper(): c for c in detected if c.get("id")}
    matched = []
    unmatched_expected = []
    extra_detected = []

    expected_badges = {c["card_badge"].upper() for c in data.get("cards") or []}
    for card in data.get("cards") or []:
        badge = card["card_badge"].upper()
        hit = by_badge.get(badge)
        if hit:
            matched.append({"card_badge": badge, "path": hit.get("path"), "scan_path": hit.get("scan_path")})
        else:
            unmatched_expected.append(badge)

    for badge, hit in by_badge.items():
        if badge not in expected_badges:
            extra_detected.append({"card_badge": badge, "path": hit.get("path")})

    return {
        "matched": matched,
        "unmatched_expected": unmatched_expected,
        "extra_detected": extra_detected,
        "detected": detected,
    }


def bind_card(
    batch_id: str,
    *,
    card_badge: str,
    mount_path: str,
    scan_path: str,
    videos: list[dict],
) -> dict:
    """Bind a detected card and register its source videos."""
    with _lock:
        data = get_batch(batch_id)
        if not data:
            raise FileNotFoundError(f"Batch not found: {batch_id}")
        if data.get("status") == "complete":
            raise ValueError("Batch is already complete")

        badge = card_badge.strip().upper()
        target = None
        for card in data.get("cards") or []:
            if card.get("card_badge", "").upper() == badge:
                target = card
                break
        if target is None:
            raise ValueError(f"Card {badge} is not in this batch CSV")

        assets = []
        for video in videos:
            path = str(video.get("path") or "")
            if not path:
                continue
            assets.append(
                {
                    "path": path,
                    "name": video.get("name") or Path(path).name,
                    "duration": video.get("duration"),
                    "size_bytes": video.get("size_bytes"),
                    "relative": video.get("relative"),
                }
            )

        target["status"] = "reviewing" if target.get("status") != "complete" else "complete"
        target["mount_path"] = mount_path
        target["scan_path"] = scan_path
        target["bound_at"] = target.get("bound_at") or _now_iso()
        target["factory"] = target.get("factory") or data.get("factory")
        # Merge assets by path (keep existing if already present)
        existing_paths = {a.get("path") for a in target.get("assets") or []}
        merged = list(target.get("assets") or [])
        for asset in assets:
            if asset["path"] not in existing_paths:
                merged.append(asset)
                existing_paths.add(asset["path"])
            else:
                for old in merged:
                    if old.get("path") == asset["path"]:
                        if asset.get("duration") is not None:
                            old["duration"] = asset["duration"]
                        if asset.get("size_bytes") is not None:
                            old["size_bytes"] = asset["size_bytes"]
        target["assets"] = merged
        _save(data)
    return get_batch_detail(batch_id)


def remove_asset(batch_id: str, video_path: str) -> dict:
    """Remove a deliberately discarded video from its batch card."""
    target_path = str(Path(video_path).expanduser().resolve())
    with _lock:
        data = get_batch(batch_id)
        if not data:
            raise FileNotFoundError(f"Batch not found: {batch_id}")
        if data.get("status") == "complete":
            raise ValueError("Batch is already complete")

        removed = False
        for card in data.get("cards") or []:
            assets = card.get("assets") or []
            kept = [
                asset
                for asset in assets
                if str(Path(str(asset.get("path") or "")).expanduser().resolve()) != target_path
            ]
            if len(kept) != len(assets):
                card["assets"] = kept
                removed = True
        if removed:
            _save(data)
    return get_batch_detail(batch_id)


def sync_asset_annotations(batch_id: str) -> dict:
    """Refresh coverage for every registered asset from on-disk sidecars."""
    with _lock:
        data = get_batch(batch_id)
        if not data:
            raise FileNotFoundError(f"Batch not found: {batch_id}")
        for card in data.get("cards") or []:
            for asset in card.get("assets") or []:
                path = Path(str(asset.get("path") or ""))
                annotation = annotation_store.load_annotation(path) if path.is_file() else None
                if annotation:
                    summary = annotation_store.coverage_summary(annotation)
                    asset["duration"] = annotation.get("duration") or asset.get("duration")
                    asset["annotation"] = {
                        "complete": summary["complete"],
                        "work_seconds": summary["work_seconds"],
                        "garbage_seconds": summary["garbage_seconds"],
                        "unreviewed_seconds": summary["unreviewed_seconds"],
                        "task_seconds": summary["task_seconds"],
                        "segment_count": summary["segment_count"],
                    }
                elif path.is_file():
                    duration = asset.get("duration")
                    asset["annotation"] = {
                        "complete": False,
                        "work_seconds": 0.0,
                        "garbage_seconds": 0.0,
                        "unreviewed_seconds": float(duration or 0),
                        "task_seconds": {},
                        "segment_count": 0,
                    }
                else:
                    asset["annotation"] = asset.get("annotation") or {
                        "complete": False,
                        "work_seconds": 0.0,
                        "garbage_seconds": 0.0,
                        "unreviewed_seconds": float(asset.get("duration") or 0),
                        "task_seconds": {},
                        "segment_count": 0,
                        "missing_file": True,
                    }
        _save(data)
    return get_batch_detail(batch_id)


def finish_card(batch_id: str, card_badge: str) -> dict:
    detail = sync_asset_annotations(batch_id)
    badge = card_badge.strip().upper()
    card = next((c for c in detail["cards"] if c.get("card_badge", "").upper() == badge), None)
    if not card:
        raise ValueError(f"Card {badge} not found in batch")

    blocking = _card_blocking(card)
    if blocking:
        raise ValueError("Card not ready: " + "; ".join(blocking))

    with _lock:
        data = get_batch(batch_id)
        for item in data.get("cards") or []:
            if item.get("card_badge", "").upper() == badge:
                item["status"] = "complete"
                item["completed_at"] = _now_iso()
        _save(data)
    return get_batch_detail(batch_id)


def complete_batch(batch_id: str) -> dict:
    detail = sync_asset_annotations(batch_id)
    blocking = detail.get("report", {}).get("blocking") or []
    if blocking:
        raise ValueError("Batch not ready:\n" + "\n".join(blocking))

    with _lock:
        data = get_batch(batch_id)
        for card in data.get("cards") or []:
            card["status"] = "complete"
            card["completed_at"] = card.get("completed_at") or _now_iso()
        data["status"] = "complete"
        data["completed_at"] = _now_iso()
        _save(data)
    return get_batch_detail(batch_id)


def _card_blocking(card: dict) -> list[str]:
    badge = card.get("card_badge") or "?"
    issues = []
    assets = card.get("assets") or []
    if not assets:
        issues.append(f"{badge}: no videos scanned yet")
        return issues
    for asset in assets:
        name = asset.get("name") or asset.get("path") or "?"
        duration = asset.get("duration")
        if duration is None or float(duration or 0) <= 0:
            issues.append(f"{badge}/{name}: unknown duration")
            continue
        ann = asset.get("annotation") or {}
        if not ann.get("complete"):
            unreviewed = float(ann.get("unreviewed_seconds") or duration or 0)
            issues.append(
                f"{badge}/{name}: incomplete "
                f"({unreviewed:.1f}s unreviewed, {ann.get('segment_count', 0)} segments)"
            )
        if ann.get("missing_file"):
            issues.append(f"{badge}/{name}: source file missing from disk")
    return issues


def compute_report(data: dict) -> dict:
    """Aggregate hours for a batch registry document."""
    cards_out = []
    task_seconds: dict[str, float] = {}
    device_raw: dict[str, float] = {}
    device_work: dict[str, float] = {}
    device_garbage: dict[str, float] = {}
    blocking: list[str] = []

    total_raw = 0.0
    total_work = 0.0
    total_garbage = 0.0
    total_unreviewed = 0.0
    video_count = 0

    for card in data.get("cards") or []:
        badge = card.get("card_badge") or "?"
        device_type = str(card.get("device_type") or "unknown")
        card_raw = 0.0
        card_work = 0.0
        card_garbage = 0.0
        card_unreviewed = 0.0
        assets = card.get("assets") or []
        if not assets and card.get("status") != "complete":
            blocking.append(f"{badge}: not scanned")
        for asset in assets:
            video_count += 1
            duration = float(asset.get("duration") or 0)
            ann = asset.get("annotation")
            if ann is None and asset.get("path"):
                path = Path(asset["path"])
                loaded = annotation_store.load_annotation(path) if path.is_file() else None
                ann = annotation_store.coverage_summary(loaded) if loaded else {
                    "complete": False,
                    "work_seconds": 0.0,
                    "garbage_seconds": 0.0,
                    "unreviewed_seconds": duration,
                    "task_seconds": {},
                    "segment_count": 0,
                }
            ann = ann or {}
            work = float(ann.get("work_seconds") or 0)
            garbage = float(ann.get("garbage_seconds") or 0)
            unreviewed = float(ann.get("unreviewed_seconds") or max(0.0, duration - work - garbage))
            card_raw += duration
            card_work += work
            card_garbage += garbage
            card_unreviewed += unreviewed
            for task, secs in (ann.get("task_seconds") or {}).items():
                task_seconds[task] = task_seconds.get(task, 0.0) + float(secs)
            if duration <= 0:
                blocking.append(f"{badge}/{asset.get('name')}: unknown duration")
            elif not ann.get("complete"):
                blocking.append(
                    f"{badge}/{asset.get('name')}: {unreviewed:.1f}s still unreviewed"
                )

        blocking.extend(
            issue
            for issue in _card_blocking(card)
            if issue not in blocking
        )

        device_raw[device_type] = device_raw.get(device_type, 0.0) + card_raw
        device_work[device_type] = device_work.get(device_type, 0.0) + card_work
        device_garbage[device_type] = device_garbage.get(device_type, 0.0) + card_garbage

        total_raw += card_raw
        total_work += card_work
        total_garbage += card_garbage
        total_unreviewed += card_unreviewed

        cards_out.append(
            {
                "card_badge": badge,
                "factory": card.get("factory"),
                "device_type": device_type,
                "device_id": card.get("device_id"),
                "status": card.get("status"),
                "video_count": len(assets),
                "raw_hours": round(card_raw / 3600.0, 4),
                "clean_hours": round(card_work / 3600.0, 4),
                "garbage_hours": round(card_garbage / 3600.0, 4),
                "unreviewed_hours": round(card_unreviewed / 3600.0, 4),
            }
        )

    # Deduplicate blocking messages
    seen = set()
    unique_blocking = []
    for item in blocking:
        if item not in seen:
            seen.add(item)
            unique_blocking.append(item)

    device_types = sorted(device_raw.keys())
    return {
        "batch_name": data.get("batch_name"),
        "factory": data.get("factory"),
        "status": data.get("status"),
        "complete": data.get("status") == "complete" and not unique_blocking,
        "blocking": unique_blocking,
        "totals": {
            "raw_hours": round(total_raw / 3600.0, 4),
            "clean_hours": round(total_work / 3600.0, 4),
            "garbage_hours": round(total_garbage / 3600.0, 4),
            "unreviewed_hours": round(total_unreviewed / 3600.0, 4),
            "video_count": video_count,
            "card_count": len(data.get("cards") or []),
            "cards_complete": sum(
                1 for c in (data.get("cards") or []) if c.get("status") == "complete"
            ),
            "task_count": len(task_seconds),
            "device_type_count": len(device_types),
        },
        "cards": cards_out,
        "tasks": [
            {"task": name, "hours": round(secs / 3600.0, 4)}
            for name, secs in sorted(task_seconds.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "devices": [
            {
                "device_type": dtype,
                "raw_hours": round(device_raw[dtype] / 3600.0, 4),
                "clean_hours": round(device_work.get(dtype, 0.0) / 3600.0, 4),
                "garbage_hours": round(device_garbage.get(dtype, 0.0) / 3600.0, 4),
            }
            for dtype in device_types
        ],
    }


def report_csv(data: dict) -> str:
    report = compute_report(data)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["section", "key", "value"])
    totals = report["totals"]
    for key, value in totals.items():
        writer.writerow(["totals", key, value])
    for card in report["cards"]:
        for key, value in card.items():
            writer.writerow(["card", f"{card['card_badge']}.{key}", value])
    for task in report["tasks"]:
        writer.writerow(["task", task["task"], task["hours"]])
    for device in report["devices"]:
        for key, value in device.items():
            if key == "device_type":
                continue
            writer.writerow(["device", f"{device['device_type']}.{key}", value])
    if report["blocking"]:
        for issue in report["blocking"]:
            writer.writerow(["blocking", "issue", issue])
    return buf.getvalue()

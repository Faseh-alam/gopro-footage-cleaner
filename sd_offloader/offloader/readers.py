"""Map three physical card readers to Reader 1 / 2 / 3."""

from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path

from .config import load_config, save_config
from .detect import list_volumes

_usb_cache: dict[str, str] = {}


def volume_letter(path: str | Path) -> str:
    drive = Path(path).drive
    return drive.replace(":", "").replace("\\", "").upper()


def usb_id_for_path(path: str | Path) -> str:
    """Stable reader identity (Windows disk PNP id), else drive letter."""
    letter = volume_letter(path)
    if letter and letter in _usb_cache:
        return _usb_cache[letter]
    if platform.system() == "Windows" and letter:
        pnp = _windows_disk_pnp(letter)
        if pnp:
            _usb_cache[letter] = pnp
            return pnp
    if letter:
        ident = f"LETTER:{letter}"
        _usb_cache[letter] = ident
        return ident
    try:
        return f"PATH:{Path(path).resolve()}"
    except OSError:
        return f"PATH:{path}"


def _windows_disk_pnp(letter: str) -> str:
    script = (
        f"$p = Get-Partition -DriveLetter {letter} -ErrorAction SilentlyContinue; "
        "if (-not $p) { return }; "
        "$d = $p | Get-Disk -ErrorAction SilentlyContinue; "
        "if (-not $d) { return }; "
        "$w = Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Index -eq $d.Number } | Select-Object -First 1; "
        "if ($w.PNPDeviceID) { $w.PNPDeviceID } "
        "elseif ($d.SerialNumber) { $d.SerialNumber }"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (result.stdout or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:240]


def saved_readers() -> dict[str, dict]:
    raw = load_config().get("card_readers") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for slot in ("1", "2", "3"):
        row = raw.get(slot)
        if row is None:
            try:
                row = raw.get(int(slot))
            except Exception:
                row = None
        if not isinstance(row, dict):
            continue
        usb = str(row.get("usb_id") or "").strip()
        if not usb:
            continue
        out[slot] = {
            "usb_id": usb,
            "label": str(row.get("label") or f"Reader {slot}"),
            "letter": str(row.get("letter") or ""),
        }
    return out


def match_reader(path: str) -> dict:
    """Return {slot, label, usb_id} for this mount, or unmatched."""
    usb = usb_id_for_path(path)
    letter = volume_letter(path)
    mapped = saved_readers()
    for slot, row in mapped.items():
        if row.get("usb_id") and usb and row["usb_id"] == usb:
            return {"slot": slot, "label": row["label"], "usb_id": usb, "mapped": True}
        if row.get("letter") and letter and row["letter"].upper() == letter:
            return {"slot": slot, "label": row["label"], "usb_id": usb, "mapped": True}
    return {"slot": "", "label": "Unmapped reader", "usb_id": usb, "mapped": False}


def map_slot(slot: int | str, path: str) -> dict:
    slot_s = str(int(slot))
    if slot_s not in {"1", "2", "3"}:
        raise ValueError("Reader slot must be 1, 2, or 3")
    if not path:
        raise ValueError("Pick a volume to map")
    usb = usb_id_for_path(path)
    letter = volume_letter(path)
    readers = dict(load_config().get("card_readers") or {})
    for other, row in list(readers.items()):
        if str(other) == slot_s or not isinstance(row, dict):
            continue
        if str(row.get("usb_id") or "") == usb and usb:
            readers.pop(other, None)
    readers[slot_s] = {
        "usb_id": usb,
        "letter": letter,
        "label": f"Reader {slot_s}",
        "path": str(Path(path)),
    }
    save_config({"card_readers": readers})
    return readers[slot_s]


def list_mappable_volumes() -> list[dict]:
    """Drive list for the Map dropdown — no PowerShell PNP (that blocked the UI)."""
    mapped = saved_readers()
    by_letter = {
        str(row.get("letter") or "").upper(): slot
        for slot, row in mapped.items()
        if row.get("letter")
    }
    rows = []
    for vol in list_volumes():
        path = str(vol.get("path") or "")
        if not path:
            continue
        if vol.get("drive_type") == "fixed" and not vol.get("is_card_candidate"):
            continue
        letter = volume_letter(path)
        slot = by_letter.get(letter, "")
        label = ""
        if slot:
            label = str((mapped.get(slot) or {}).get("label") or f"Reader {slot}")
        rows.append(
            {
                **{k: vol.get(k) for k in ("path", "label", "is_card_candidate", "card_id")},
                "reader_slot": slot,
                "reader_label": label,
                "usb_id": "",
            }
        )
    return rows


def card_reader_status() -> dict:
    # Do not scan cards here. find_card_volumes / per-drive PNP hung the page
    # so Reader 1/2/3 never painted.
    return {
        "mapped": saved_readers(),
        "volumes": list_mappable_volumes(),
    }

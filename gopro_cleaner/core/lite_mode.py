"""Detect low-RAM machines and expose lightweight snapshot settings."""

from __future__ import annotations

import os
import platform
import subprocess

_LITE_RAM_GB_THRESHOLD = 10.0
_lite_cached: bool | None = None


def system_ram_gb() -> float | None:
    if platform.system() == "Windows":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullTotalPhys / (1024**3)
        except Exception:
            return None
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                check=True,
            )
            return int(out.stdout.strip()) / (1024**3)
        except Exception:
            return None
    return None


def lite_mode_enabled() -> bool:
    global _lite_cached
    if _lite_cached is not None:
        return _lite_cached

    env = os.environ.get("GOPRO_LITE_MODE", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        _lite_cached = True
        return True
    if env in {"0", "false", "no", "off"}:
        _lite_cached = False
        return False

    ram = system_ram_gb()
    _lite_cached = ram is not None and ram <= _LITE_RAM_GB_THRESHOLD
    return _lite_cached


def performance_config() -> dict:
    lite = lite_mode_enabled()
    ram = system_ram_gb()
    if lite:
        return {
            "lite_mode": True,
            "ram_gb": round(ram, 1) if ram else None,
            "max_snapshots": 8,
            "snapshot_width": 120,
            "interval_min_sec": 20.0,
            "label_preview_count": 5,
            "prefetch": False,
            "snapshot_poll_ms": 2500,
            "trim_poll_ms": 2500,
            "hint": "Lite mode — fewer snapshots, no background loading (low RAM PC)",
        }
    return {
        "lite_mode": False,
        "ram_gb": round(ram, 1) if ram else None,
        "max_snapshots": 24,
        "snapshot_width": 160,
        "interval_min_sec": 8.0,
        "label_preview_count": 8,
        "prefetch": True,
        "snapshot_poll_ms": 1000,
        "trim_poll_ms": 1200,
        "hint": "",
    }

"""Cross-platform removable / local volume listing."""

from __future__ import annotations

import os
import string
import sys
from pathlib import Path


def list_volume_roots() -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    home = str(Path.home())
    items.append({"name": "Home", "path": home})

    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:/")
            if root.exists():
                items.append({"name": f"{letter}:", "path": str(root)})
        return items

    volumes = Path("/Volumes")
    if volumes.exists():
        for entry in sorted(volumes.iterdir(), key=lambda p: p.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                items.append({"name": entry.name, "path": str(entry)})
    return items


def normalize_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()

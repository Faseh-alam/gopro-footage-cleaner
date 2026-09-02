"""SSD placement: 10 GB reserve, one card → one SSD, pack queued cards."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sd_offloader"))

from offloader import space  # noqa: E402

GB = 1024**3
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssd1"
        ssd2 = Path(tmp) / "ssd2"
        ssd1.mkdir()
        ssd2.mkdir()
        free = {space.path_key(ssd1): 100 * GB, space.path_key(ssd2): 200 * GB}

        def fake_free(path):
            return free[space.path_key(path)]

        with patch.object(space, "volume_free_bytes", side_effect=fake_free):
            # 100 GB free, 40 GB card → 60 GB remain >= 10 → SSD1
            path, _ = space.pick_ssd_for_bytes(
                ssd1=str(ssd1), ssd2=str(ssd2), needed_bytes=40 * GB
            )
            check("40 GB card → SSD1", space.path_key(path) == space.path_key(ssd1), path)

            # After 40+40 committed (80), third 40: usable 20, 20-40 < 10 → SSD2
            path, _ = space.pick_ssd_for_bytes(
                ssd1=str(ssd1),
                ssd2=str(ssd2),
                needed_bytes=40 * GB,
                reserved_bytes={space.path_key(ssd1): 80 * GB},
            )
            check("third 40 GB card → SSD2", space.path_key(path) == space.path_key(ssd2), path)

            # 80 GB free, 60 GB card: 20 remain >= 10 → SSD1
            free[space.path_key(ssd1)] = 80 * GB
            path, _ = space.pick_ssd_for_bytes(
                ssd1=str(ssd1), ssd2=str(ssd2), needed_bytes=60 * GB
            )
            check("60 into 80 → SSD1 (20 >= 10 reserve)", space.path_key(path) == space.path_key(ssd1))

            # 80 GB free, 75 GB card: 5 remain < 10 → SSD2
            path, _ = space.pick_ssd_for_bytes(
                ssd1=str(ssd1), ssd2=str(ssd2), needed_bytes=75 * GB
            )
            check("75 into 80 → SSD2 (would leave 5 GB)", space.path_key(path) == space.path_key(ssd2))

            neither = False
            try:
                space.pick_ssd_for_bytes(
                    ssd1=str(ssd1),
                    ssd2=str(ssd2),
                    needed_bytes=195 * GB,
                    reserved_bytes={},
                )
            except RuntimeError:
                neither = True
            # ssd2 has 200, 200-195=5 < 10, ssd1 80 too small
            check("too large for both with 10 GB reserve → error", neither)

            check("reserve is 10 GB", space.RESERVE_BYTES == 10 * GB, str(space.RESERVE_BYTES))
            check(
                "fits_with_reserve 20-10",
                space.fits_with_reserve(80 * GB, 60 * GB) is True,
            )
            check(
                "rejects eating reserve",
                space.fits_with_reserve(80 * GB, 75 * GB) is False,
            )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("SSD 10 GB placement checks passed.")


if __name__ == "__main__":
    main()

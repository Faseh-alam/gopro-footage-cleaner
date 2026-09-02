"""Unique batches, MP4+JSON pairing, freeze/unfreeze by batch name."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sd_offloader"))

from offloader import batches, engine, inventory, space  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def test_next_batch_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssd1" / "Batches"
        ssd2 = Path(tmp) / "ssd2" / "Batches"
        ssd1.mkdir(parents=True)
        ssd2.mkdir(parents=True)
        (ssd1 / "batch 6").mkdir()
        name = batches.next_batch_name(str(ssd1.parent), str(ssd2.parent), seed="batch 6")
        check("seed already on SSD1 → next number", name == "batch 7", name)
        name = batches.next_batch_name(str(ssd1.parent), str(ssd2.parent), seed="batch 6", extra={"batch 7"})
        check("extra live name skipped", name == "batch 8", name)
        name = batches.next_batch_name(str(ssd1.parent), str(ssd2.parent), seed="night 3")
        check("unused seed kept", name == "night 3", name)
        check("batch_number night 3", batches.batch_number("night 3") == 3)


def test_unpaired() -> None:
    files = [
        {"rel": "GX010001.MP4", "embed_json": r"C:\card\GX010001.JSON"},
        {"rel": "GX010002.MP4", "embed_json": ""},
        {"rel": "GX010001.JSON"},
    ]
    missing = inventory.unpaired_mp4s(files)
    check("flags MP4 without sidecar", missing == ["GX010002.MP4"], str(missing))
    with tempfile.TemporaryDirectory() as tmp:
        mp4 = Path(tmp) / "GX010001.MP4"
        js = Path(tmp) / "GX010001.JSON"
        mp4.write_bytes(b"x")
        js.write_text("{}", encoding="utf-8")
        check("sidecar_for_mp4 finds JSON", inventory.sidecar_for_mp4(mp4) == js)


def test_unfreeze_by_batch_name() -> None:
    with engine._lock:
        engine._session["ssd1"] = "E:\\ssd1"
        engine._session["ssd2"] = "F:\\ssd2"
        engine._session["disk_batches"] = {"e:\\ssd1": "batch 9", "f:\\ssd2": "batch 10"}
        engine._session["frozen_disks"] = {"e:\\ssd1": "batch 9"}
    engine.on_batch_deleted([r"E:\ssd1\Batches\batch 9"], "batch 9")
    with engine._lock:
        frozen = dict(engine._session.get("frozen_disks") or {})
        live = dict(engine._session.get("disk_batches") or {})
    check("unfreeze matching batch only", "e:\\ssd1" not in frozen, str(frozen))
    check("other disk batch kept", live.get("f:\\ssd2") == "batch 10", str(live))
    check("deleted batch not live", "batch 9" not in live.values(), str(live))


def test_freeze_skip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssd1"
        ssd2 = Path(tmp) / "ssd2"
        ssd1.mkdir()
        ssd2.mkdir()
        free = {space.path_key(ssd1): 20 * 1024**3, space.path_key(ssd2): 200 * 1024**3}
        with engine._lock:
            engine._session["ssd1"] = str(ssd1)
            engine._session["ssd2"] = str(ssd2)
            engine._session["mode"] = "ssd_and_aws"
            engine._session["s3_uri"] = "s3://bucket/footage/"
            engine._session["disk_batches"] = {space.path_key(ssd1): "batch 11"}
            engine._session["frozen_disks"] = {space.path_key(ssd1): "batch 11"}
            engine._session["batch"] = "batch 11"

        def fake_free(path):
            return free[space.path_key(path)]

        with patch.object(space, "volume_free_bytes", side_effect=fake_free):
            picked, dest, batch = engine._assign_ssd_and_batch(
                needed=40 * 1024**3,
                ssd1=str(ssd1),
                ssd2=str(ssd2),
                seed="batch 11",
                exclude_card="",
                resume_dest=None,
            )
        check("frozen SSD1 → SSD2", space.path_key(picked) == space.path_key(ssd2), picked)
        check("SSD2 batch not shared", batch != "batch 11", batch)


def main() -> None:
    test_next_batch_name()
    test_unpaired()
    test_unfreeze_by_batch_name()
    test_freeze_skip()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("Remaining offloader checks passed.")


if __name__ == "__main__":
    main()

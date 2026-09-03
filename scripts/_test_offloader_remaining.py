"""Unique batches, MP4+JSON pairing, freeze/unfreeze, shared SSD batch cycles."""

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


def _silence_persist():
    return patch.object(engine, "_persist_disk_state")


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


def test_successor_and_resume() -> None:
    check("batch01 → batch02", batches.successor_batch_name("batch01") == "batch02")
    check("batch 6 → batch 7", batches.successor_batch_name("batch 6") == "batch 7")
    check(
        "both empty → seed batch01",
        batches.resume_or_next_batch(seed="batch01") == "batch01",
    )
    check(
        "SSD1 finished batch01 → batch02",
        batches.resume_or_next_batch(seed="batch01", completed="batch01") == "batch02",
    )
    check(
        "folder still on disk stays on batch01",
        batches.resume_or_next_batch(
            seed="batch01", completed="", folders={"batch01"}
        )
        == "batch01",
    )
    check(
        "live wins over seed",
        batches.resume_or_next_batch(seed="batch01", live="batch01", completed="batch01")
        == "batch01",
    )


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
        engine._session["disk_completed"] = {}
        engine._session["frozen_disks"] = {"e:\\ssd1": "batch 9"}
    with _silence_persist():
        engine.on_batch_deleted([r"E:\ssd1\Batches\batch 9"], "batch 9")
    with engine._lock:
        frozen = dict(engine._session.get("frozen_disks") or {})
        live = dict(engine._session.get("disk_batches") or {})
        completed = dict(engine._session.get("disk_completed") or {})
    check("unfreeze matching batch only", "e:\\ssd1" not in frozen, str(frozen))
    check("other disk batch kept", live.get("f:\\ssd2") == "batch 10", str(live))
    check("deleted batch not live on that disk", live.get("e:\\ssd1") != "batch 9", str(live))
    check("completed recorded for deleted disk", completed.get("e:\\ssd1") == "batch 9", str(completed))


def test_shared_cycle_delete_does_not_advance_other_ssd() -> None:
    print("\n[shared cycle] SSD1 delete must not pull SSD2 off batch01")
    with engine._lock:
        engine._session["ssd1"] = "E:\\ssd1"
        engine._session["ssd2"] = "F:\\ssd2"
        engine._session["disk_batches"] = {"e:\\ssd1": "batch01", "f:\\ssd2": "batch01"}
        engine._session["disk_completed"] = {}
        engine._session["frozen_disks"] = {"e:\\ssd1": "batch01"}
    with _silence_persist():
        engine.on_batch_deleted([r"E:\ssd1\Batches\batch01"], "batch01")
    with engine._lock:
        live = dict(engine._session.get("disk_batches") or {})
        completed = dict(engine._session.get("disk_completed") or {})
        frozen = dict(engine._session.get("frozen_disks") or {})
    check("SSD2 still on batch01", live.get("f:\\ssd2") == "batch01", str(live))
    check("SSD1 live cleared", "e:\\ssd1" not in live or live.get("e:\\ssd1") != "batch01", str(live))
    check("SSD1 completed batch01", completed.get("e:\\ssd1") == "batch01", str(completed))
    check("SSD2 not completed", not completed.get("f:\\ssd2"), str(completed))
    check("SSD2 not frozen", "f:\\ssd2" not in frozen, str(frozen))


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
            engine._session["disk_completed"] = {}
            engine._session["frozen_disks"] = {space.path_key(ssd1): "batch 11"}
            engine._session["batch"] = "batch 11"

        def fake_free(path):
            return free[space.path_key(path)]

        with patch.object(space, "volume_free_bytes", side_effect=fake_free):
            with _silence_persist():
                picked, dest, batch = engine._assign_ssd_and_batch(
                    needed=40 * 1024**3,
                    ssd1=str(ssd1),
                    ssd2=str(ssd2),
                    seed="batch 11",
                    exclude_card="",
                    resume_dest=None,
                )
        check("frozen SSD1 → SSD2", space.path_key(picked) == space.path_key(ssd2), picked)
        check("SSD2 shares the same cycle", batch == "batch 11", batch)


def test_both_ssds_start_and_stagger_to_batch02() -> None:
    print("\n[shared cycle] both start batch01; whoever finishes first goes to batch02")
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssd1"
        ssd2 = Path(tmp) / "ssd2"
        ssd1.mkdir()
        ssd2.mkdir()
        k1, k2 = space.path_key(ssd1), space.path_key(ssd2)
        with engine._lock:
            engine._session["ssd1"] = str(ssd1)
            engine._session["ssd2"] = str(ssd2)
            engine._session["disk_batches"] = {}
            engine._session["disk_completed"] = {}
            engine._session["frozen_disks"] = {}
            engine._session["batch"] = "batch01"
        with _silence_persist():
            b1 = engine._batch_for_ssd(str(ssd1), seed="batch01")
            b2 = engine._batch_for_ssd(str(ssd2), seed="batch01")
        check("both SSDs start at batch01", b1 == "batch01" and b2 == "batch01", f"{b1}/{b2}")

        with _silence_persist():
            root1 = space.batch_root(ssd1, "batch01")
            if root1.exists():
                root1.rmdir()
            engine.on_batch_deleted([str(root1)], "batch01")
            next1 = engine._batch_for_ssd(str(ssd1), seed="batch01")
            still2 = engine._batch_for_ssd(str(ssd2), seed="batch01")
        check("SSD1 finishes first → batch02", next1 == "batch02", next1)
        check("SSD2 still batch01 while uploading", still2 == "batch01", still2)

        with engine._lock:
            engine._session["disk_batches"] = {k1: "batch02", k2: "batch01"}
            engine._session["disk_completed"] = {k1: "batch01"}
            engine._session["frozen_disks"] = {k2: "batch01"}
        with _silence_persist():
            root2 = space.batch_root(ssd2, "batch01")
            if root2.exists():
                root2.rmdir()
            engine.on_batch_deleted([str(root2)], "batch01")
            next2 = engine._batch_for_ssd(str(ssd2), seed="batch01")
            stay1 = engine._batch_for_ssd(str(ssd1), seed="batch01")
        check("SSD2 finishes later → batch02", next2 == "batch02", next2)
        check("SSD1 stays on batch02", stay1 == "batch02", stay1)
        check("same cycle number after both finish", next2 == stay1 == "batch02")


def test_ssd2_finishes_first() -> None:
    print("\n[shared cycle] SSD2 finishes before SSD1")
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssd1"
        ssd2 = Path(tmp) / "ssd2"
        ssd1.mkdir()
        ssd2.mkdir()
        with engine._lock:
            engine._session["ssd1"] = str(ssd1)
            engine._session["ssd2"] = str(ssd2)
            engine._session["disk_batches"] = {
                space.path_key(ssd1): "batch01",
                space.path_key(ssd2): "batch01",
            }
            engine._session["disk_completed"] = {}
            engine._session["frozen_disks"] = {space.path_key(ssd2): "batch01"}
        with _silence_persist():
            root2 = space.batch_root(ssd2, "batch01")
            if root2.exists():
                root2.rmdir()
            engine.on_batch_deleted([str(root2)], "batch01")
            next2 = engine._batch_for_ssd(str(ssd2), seed="batch01")
            still1 = engine._batch_for_ssd(str(ssd1), seed="batch01")
        check("SSD2 → batch02", next2 == "batch02", next2)
        check("SSD1 still batch01", still1 == "batch01", still1)


def test_failed_upload_or_verify_does_not_advance() -> None:
    print("\n[shared cycle] failed upload/verify does not advance")
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssd1"
        ssd2 = Path(tmp) / "ssd2"
        ssd1.mkdir()
        ssd2.mkdir()
        k1 = space.path_key(ssd1)
        with engine._lock:
            engine._session["ssd1"] = str(ssd1)
            engine._session["ssd2"] = str(ssd2)
            engine._session["disk_batches"] = {k1: "batch01"}
            engine._session["disk_completed"] = {}
            engine._session["frozen_disks"] = {k1: "batch01"}
        with _silence_persist():
            # Upload/verify failed → no on_batch_deleted
            again = engine._batch_for_ssd(str(ssd1), seed="batch01")
        check("failed upload keeps batch01", again == "batch01", again)
        with engine._lock:
            completed = dict(engine._session.get("disk_completed") or {})
            frozen = dict(engine._session.get("frozen_disks") or {})
        check("not marked completed", completed.get(k1) != "batch01", str(completed))
        check("still frozen until verify+delete", frozen.get(k1) == "batch01", str(frozen))


def test_restart_does_not_reset_cycle() -> None:
    print("\n[shared cycle] restart keeps completed + live batches")
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssd1"
        ssd2 = Path(tmp) / "ssd2"
        ssd1.mkdir()
        ssd2.mkdir()
        (ssd2 / "Batches" / "batch01").mkdir(parents=True)
        k1, k2 = space.path_key(ssd1), space.path_key(ssd2)
        cfg = {
            "disk_batches": {k2: "batch01"},
            "disk_completed": {k1: "batch01"},
            "frozen_disks": {},
        }
        with engine._lock:
            engine._session["ssd1"] = str(ssd1)
            engine._session["ssd2"] = str(ssd2)
            engine._session["disk_batches"] = dict(cfg["disk_batches"])
            engine._session["disk_completed"] = dict(cfg["disk_completed"])
            engine._session["frozen_disks"] = {}
            engine._session["batch"] = "batch01"
        with _silence_persist():
            engine._restore_disk_batches_from_folders(str(ssd1), str(ssd2), seed="batch01")
            after_restart_1 = engine._batch_for_ssd(str(ssd1), seed="batch01")
            after_restart_2 = engine._batch_for_ssd(str(ssd2), seed="batch01")
        check("restart SSD1 continues at batch02", after_restart_1 == "batch02", after_restart_1)
        check("restart SSD2 resumes batch01 folder", after_restart_2 == "batch01", after_restart_2)


def main() -> None:
    test_next_batch_name()
    test_successor_and_resume()
    test_unpaired()
    test_unfreeze_by_batch_name()
    test_shared_cycle_delete_does_not_advance_other_ssd()
    test_freeze_skip()
    test_both_ssds_start_and_stagger_to_batch02()
    test_ssd2_finishes_first()
    test_failed_upload_or_verify_does_not_advance()
    test_restart_does_not_reset_cycle()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("Remaining offloader checks passed.")


if __name__ == "__main__":
    main()

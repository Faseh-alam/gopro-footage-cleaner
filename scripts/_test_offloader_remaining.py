"""Unique batches, MP4+JSON pairing, freeze/unfreeze, shared SSD batch cycles."""

from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sd_offloader"))

from offloader import aws_upload, batches, engine, inventory, space  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def _reset_session(**fields) -> None:
    with engine._lock:
        engine._session.update(
            {
                "active": False,
                "batch": "batch01",
                "mode": "ssd_only",
                "ssd1": "",
                "ssd2": "",
                "s3_uri": "",
                "disk_batches": {},
                "disk_completed": {},
                "closed_batches": {},
                "frozen_disks": {},
            }
        )
        engine._session.update(fields)


def _put_clip(ssd: Path, batch: str, name: str = "GX010001.MP4") -> Path:
    folder = space.batch_root(ssd, batch)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"footage")
    return path


@contextmanager
def _silence_engine():
    with patch.object(engine, "_persist_disk_state"), patch.object(
        engine, "_pump_closed_uploads"
    ):
        yield


def _silence_persist():
    return _silence_engine()


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
    _reset_session(
        ssd1="E:\\ssd1",
        ssd2="F:\\ssd2",
        disk_batches={"e:\\ssd1": "batch 9", "f:\\ssd2": "batch 10"},
        frozen_disks={"e:\\ssd1": "batch 9"},
    )
    with _silence_engine():
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
    _reset_session(
        ssd1="E:\\ssd1",
        ssd2="F:\\ssd2",
        disk_batches={"e:\\ssd1": "batch01", "f:\\ssd2": "batch01"},
        frozen_disks={"e:\\ssd1": "batch01"},
    )
    with _silence_engine():
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
        _reset_session(
            ssd1=str(ssd1),
            ssd2=str(ssd2),
            mode="ssd_and_aws",
            s3_uri="s3://bucket/footage/",
            disk_batches={space.path_key(ssd1): "batch 11"},
            batch="batch 11",
            frozen_disks={space.path_key(ssd1): "batch 11"},
        )

        def fake_free(path):
            return free[space.path_key(path)]

        with patch.object(space, "volume_free_bytes", side_effect=fake_free):
            with _silence_engine():
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
        _reset_session(
            ssd1=str(ssd1),
            ssd2=str(ssd2),
            batch="batch01",
        )
        with _silence_engine():
            b1 = engine._batch_for_ssd(str(ssd1), seed="batch01")
            b2 = engine._batch_for_ssd(str(ssd2), seed="batch01")
        check("both SSDs start at batch01", b1 == "batch01" and b2 == "batch01", f"{b1}/{b2}")

        with _silence_engine():
            root1 = space.batch_root(ssd1, "batch01")
            if root1.exists():
                root1.rmdir()
            engine.on_batch_deleted([str(root1)], "batch01")
            next1 = engine._batch_for_ssd(str(ssd1), seed="batch01")
            still2 = engine._batch_for_ssd(str(ssd2), seed="batch01")
        check("SSD1 finishes first → batch02", next1 == "batch02", next1)
        check("SSD2 still batch01 while uploading", still2 == "batch01", still2)

        _reset_session(
            ssd1=str(ssd1),
            ssd2=str(ssd2),
            disk_batches={k1: "batch02", k2: "batch01"},
            disk_completed={k1: "batch01"},
            frozen_disks={k2: "batch01"},
            batch="batch01",
        )
        with _silence_engine():
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
        _reset_session(
            ssd1=str(ssd1),
            ssd2=str(ssd2),
            disk_batches={
                space.path_key(ssd1): "batch01",
                space.path_key(ssd2): "batch01",
            },
            frozen_disks={space.path_key(ssd2): "batch01"},
        )
        with _silence_engine():
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
        _reset_session(
            ssd1=str(ssd1),
            ssd2=str(ssd2),
            disk_batches={k1: "batch01"},
            frozen_disks={k1: "batch01"},
        )
        with _silence_engine():
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
        _reset_session(
            ssd1=str(ssd1),
            ssd2=str(ssd2),
            disk_batches={k2: "batch01"},
            disk_completed={k1: "batch01"},
            batch="batch01",
        )
        with _silence_engine():
            engine._restore_disk_batches_from_folders(str(ssd1), str(ssd2), seed="batch01")
            after_restart_1 = engine._batch_for_ssd(str(ssd1), seed="batch01")
            after_restart_2 = engine._batch_for_ssd(str(ssd2), seed="batch01")
        check("restart SSD1 continues at batch02", after_restart_1 == "batch02", after_restart_1)
        check("restart SSD2 resumes batch01 folder", after_restart_2 == "batch01", after_restart_2)


def test_manual_batch_completed_workflow() -> None:
    print("\n[batch completed] manual close, queue, cleanup isolation, restart")
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssd1"
        ssd2 = Path(tmp) / "ssd2"
        ssd1.mkdir()
        ssd2.mkdir()
        k1, k2 = space.path_key(ssd1), space.path_key(ssd2)
        _put_clip(ssd1, "batch01")
        _put_clip(ssd2, "batch01")
        _reset_session(
            ssd1=str(ssd1),
            ssd2=str(ssd2),
            mode="ssd_and_aws",
            s3_uri="s3://bucket/footage/",
            disk_batches={k1: "batch01", k2: "batch01"},
            batch="batch01",
        )
        with _silence_engine():
            result = engine.close_active_batch(str(ssd2))
        check("1. partial SSD2 batch can close", result["closed"] == "batch01", str(result))
        check("2. next active on SSD2 is batch02", result["active"] == "batch02", str(result))
        check(
            "3. closed folder still on disk",
            space.batch_root(ssd2, "batch01").is_dir(),
        )
        with engine._lock:
            closed = dict(engine._session.get("closed_batches") or {})
            live = dict(engine._session.get("disk_batches") or {})
        check("3b. batch01 queued for AWS", "batch01" in (closed.get(k2) or []), str(closed))
        check("SSD2 live is batch02", live.get(k2) == "batch02", str(live))
        check("11. SSD1 still on batch01", live.get(k1) == "batch01", str(live))
        check("11b. SSD1 closed list empty", not closed.get(k1), str(closed))

        _put_clip(ssd2, "batch02")
        with _silence_engine():
            picked, dest, batch = engine._assign_ssd_and_batch(
                needed=1024,
                ssd1=str(ssd1),
                ssd2=str(ssd2),
                seed="batch01",
                exclude_card="",
                resume_dest=None,
            )
        # SSD1 still preferred and has space → new cards on SSD1 batch01.
        # Force a card onto SSD2 by making SSD1 too small.
        free = {k1: 1 * 1024**3, k2: 200 * 1024**3}

        def fake_free(path):
            return free[space.path_key(path)]

        with patch.object(engine, "_maybe_auto_upload_disk"):
            with patch.object(space, "volume_free_bytes", side_effect=fake_free):
                with _silence_engine():
                    picked, dest, batch = engine._assign_ssd_and_batch(
                        needed=40 * 1024**3,
                        ssd1=str(ssd1),
                        ssd2=str(ssd2),
                        seed="batch01",
                        exclude_card="",
                        resume_dest=None,
                    )
        check("4. new cards go to SSD2 batch02", batch == "batch02", f"{picked} {batch} {dest}")
        check("4b. dest is batch02 folder", dest.name == "batch02", str(dest))
        check("4c. batch01 still present while batch02 is active", space.batch_root(ssd2, "batch01").is_dir())

        with _silence_engine():
            engine.close_active_batch(str(ssd2))
        with engine._lock:
            closed = dict(engine._session.get("closed_batches") or {})
            live = dict(engine._session.get("disk_batches") or {})
        check(
            "5. two closed batches while third is active",
            (closed.get(k2) or []) == ["batch01", "batch02"] and live.get(k2) == "batch03",
            f"{closed.get(k2)} live={live.get(k2)}",
        )

        started: list[str] = []
        jobs: list[dict] = []

        def fake_start(**kwargs):
            name = str(kwargs.get("batch_name") or "")
            started.append(name)
            ssd_a = str(kwargs.get("ssd1") or "")
            ssd_b = str(kwargs.get("ssd2") or "")
            owner = ssd_a or ssd_b
            job = {
                "id": f"job-{len(started)}",
                "status": "running",
                "batch": name,
                "sources": [str(space.batch_root(owner, name))],
            }
            jobs.append(job)
            return job

        _put_clip(ssd2, "batch01")
        _put_clip(ssd2, "batch02")
        with patch.object(engine, "_persist_disk_state"):
            with patch.object(aws_upload, "start_batch_upload", side_effect=fake_start):
                with patch.object(aws_upload, "list_jobs", return_value=[]):
                    engine._pump_closed_uploads()
                check("6. oldest closed batch uploads first", started == ["batch01"], str(started))
                with patch.object(aws_upload, "list_jobs", return_value=list(jobs)):
                    engine._pump_closed_uploads()
                check("6b. second closed batch waits while first uploads", started == ["batch01"], str(started))
                jobs[0]["status"] = "completed"
                with patch.object(aws_upload, "list_jobs", return_value=list(jobs)):
                    engine._pump_closed_uploads()
                check("6c. next closed batch uploads independently", started == ["batch01", "batch02"], str(started))

        with engine._lock:
            live_before = dict(engine._session.get("disk_batches") or {})
            closed_before = dict(engine._session.get("closed_batches") or {})
        with _silence_engine():
            engine.on_batch_deleted([str(space.batch_root(ssd2, "batch01"))], "batch01")
        with engine._lock:
            live = dict(engine._session.get("disk_batches") or {})
            closed = dict(engine._session.get("closed_batches") or {})
            completed = dict(engine._session.get("disk_completed") or {})
        check("7. cleanup removes only batch01 from closed", "batch01" not in (closed.get(k2) or []), str(closed))
        check("7b. batch02 still closed", "batch02" in (closed.get(k2) or []), str(closed))
        check("7c. active batch03 unchanged", live.get(k2) == "batch03", str(live))
        check("7d. SSD1 untouched by SSD2 cleanup", live.get(k1) == live_before.get(k1), str(live))
        check("7e. completed records batch01", completed.get(k2) == "batch01", str(completed))
        del live_before, closed_before

        # Failed upload/verify must not delete or advance other batches.
        with engine._lock:
            ssd1_live = str((engine._session.get("disk_batches") or {}).get(k1) or "batch01")
        _put_clip(ssd1, ssd1_live)
        with _silence_engine():
            engine.close_active_batch(str(ssd1))
        with engine._lock:
            live_fail = dict(engine._session.get("disk_batches") or {})
            closed_fail = dict(engine._session.get("closed_batches") or {})
        folder01 = space.batch_root(ssd1, "batch01")
        check("8. failed upload keeps folder", folder01.is_dir())
        check("8b. batch01 still closed for retry", "batch01" in (closed_fail.get(k1) or []), str(closed_fail))
        check("8c. SSD1 already on next active batch", live_fail.get(k1) == "batch02", str(live_fail))
        check("9. failed verify does not delete batch01", folder01.is_dir())
        check("9b. SSD2 batches unchanged by SSD1 close", live_fail.get(k2) == "batch03", str(live_fail))

        # Restart recovery of active + closed + uploading.
        _reset_session(
            ssd1=str(ssd1),
            ssd2=str(ssd2),
            mode="ssd_and_aws",
            s3_uri="s3://bucket/footage/",
            disk_batches={k1: "batch02", k2: "batch03"},
            closed_batches={k1: ["batch01"], k2: ["batch01", "batch02"]},
            frozen_disks={k2: "batch01"},
            batch="batch01",
        )
        space.batch_root(ssd1, "batch02").mkdir(parents=True, exist_ok=True)
        space.batch_root(ssd2, "batch03").mkdir(parents=True, exist_ok=True)
        with _silence_engine():
            engine._restore_disk_batches_from_folders(str(ssd1), str(ssd2), seed="batch01")
            r1 = engine._batch_for_ssd(str(ssd1), seed="batch01")
            r2 = engine._batch_for_ssd(str(ssd2), seed="batch01")
        with engine._lock:
            closed = dict(engine._session.get("closed_batches") or {})
            frozen = dict(engine._session.get("frozen_disks") or {})
        check("10. restart keeps SSD1 active batch02", r1 == "batch02", r1)
        check("10b. restart keeps SSD2 active batch03", r2 == "batch03", r2)
        check(
            "10c. restart keeps closed lists",
            (closed.get(k1) or []) == ["batch01"] and (closed.get(k2) or []) == ["batch01", "batch02"],
            str(closed),
        )
        check("10d. restart keeps uploading marker", frozen.get(k2) == "batch01", str(frozen))

        # Cross-SSD isolation + numbering.
        _put_clip(ssd1, "batch02")
        with engine._lock:
            live_ssd2 = dict(engine._session.get("disk_batches") or {})
            closed_ssd2 = dict(engine._session.get("closed_batches") or {})
        with _silence_engine():
            engine.close_active_batch(str(ssd1))
        with engine._lock:
            live = dict(engine._session.get("disk_batches") or {})
            closed = dict(engine._session.get("closed_batches") or {})
        check("12. SSD1 close does not change SSD2 live", live.get(k2) == live_ssd2.get(k2), str(live))
        check("12b. SSD1 close does not change SSD2 closed", closed.get(k2) == closed_ssd2.get(k2), str(closed))
        check("13. SSD1 next is batch03, not skipped to 04", live.get(k1) == "batch03", str(live))

        # Automatic full-disk close still works.
        _reset_session(
            ssd1=str(ssd1),
            ssd2=str(ssd2),
            mode="ssd_and_aws",
            s3_uri="s3://bucket/footage/",
            disk_batches={k1: "batch01", k2: "batch01"},
            batch="batch01",
        )
        _put_clip(ssd1, "batch01")
        with _silence_engine():
            engine._maybe_auto_upload_disk(str(ssd1))
        with engine._lock:
            live = dict(engine._session.get("disk_batches") or {})
            closed = dict(engine._session.get("closed_batches") or {})
        check("14. auto-full closes current batch", "batch01" in (closed.get(k1) or []), str(closed))
        check("14b. auto-full opens next batch", live.get(k1) == "batch02", str(live))
        check("14c. auto-full does not move SSD2", live.get(k2) == "batch01", str(live))
        check("14d. auto-full does not delete folder", space.batch_root(ssd1, "batch01").is_dir())


def test_ui_resolver_and_empty_close() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssd1"
        ssd2 = Path(tmp) / "ssd2"
        ssd1.mkdir()
        ssd2.mkdir()
        _reset_session(ssd1=str(ssd1), ssd2=str(ssd2), disk_batches={space.path_key(ssd1): "batch01"})
        with _silence_engine():
            try:
                engine.close_active_batch(str(ssd1))
                empty_ok = False
            except ValueError as exc:
                empty_ok = "no footage" in str(exc).lower() or "add SD cards" in str(exc)
        check("empty active batch refuses close", empty_ok)
        _put_clip(ssd1, "batch01")
        with _silence_engine():
            result = engine.close_active_batch_for_ui("1")
        check("UI slot 1 closes SSD1", result["closed"] == "batch01" and result["active"] == "batch02", str(result))
        with _silence_engine():
            try:
                engine.close_active_batch_for_ui("2")
                ssd2_empty = True
            except ValueError:
                ssd2_empty = False
        check("UI slot 2 cannot close empty SSD2", ssd2_empty is False)


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
    test_manual_batch_completed_workflow()
    test_ui_resolver_and_empty_close()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        raise SystemExit(1)
    print("Remaining offloader checks passed.")


if __name__ == "__main__":
    main()

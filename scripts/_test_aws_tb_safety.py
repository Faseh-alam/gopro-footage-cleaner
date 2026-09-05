"""v1.9.11 TB / dual-SSD S3 safety: timeout, slack, S3 -1, watchdog, auto-delete."""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "sd_offloader"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from offloader import aws_upload as a  # noqa: E402
from offloader import engine  # noqa: E402
from offloader.config import BATCHES_SUBDIR  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def test_timeout_constant() -> None:
    print("\n[1] verify timeout is 1800s, unused for upload")
    check("AWS_VERIFY_TIMEOUT_SECONDS is 30 minutes", a.AWS_VERIFY_TIMEOUT_SECONDS == 1800)


def test_size_slack() -> None:
    print("\n[2] relative size slack: 1 MiB floor, 0.01% of large totals")
    floor = 1024 * 1024
    check("tiny batch uses 1 MiB floor", a.size_tolerance_bytes(100) == floor)
    tb = 3_200_000_000_000
    slack = a.size_tolerance_bytes(tb)
    check("3.2e12 bytes uses 0.01%", slack == max(floor, int(tb * 0.0001)), str(slack))
    check("exact match PASSes", a.sizes_within_tolerance(tb, tb))
    check(
        "inside slack PASSes",
        a.sizes_within_tolerance(tb, tb + slack),
    )
    check(
        "outside slack FAILs",
        not a.sizes_within_tolerance(tb, tb + slack + 1),
    )


def test_dir_bytes_manifest() -> None:
    print("\n[3] _dir_bytes prefers complete recorded sizes, else walks")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "a.bin").write_bytes(b"12345")
        (root / "b.bin").write_bytes(b"abcdefghij")
        walked = a._dir_bytes(root)
        check("walk sums live sizes", walked == 15, str(walked))
        check(
            "complete recorded list used",
            a._dir_bytes(root, recorded_sizes=[15, 20]) == 35,
        )
        check(
            "wrong count falls back to walk",
            a._dir_bytes(root, recorded_sizes=[99]) == 15,
        )


def test_choose_upload_sources() -> None:
    print("\n[4] never merge two SSDs into one S3 job")
    with tempfile.TemporaryDirectory() as tmp:
        ssd1 = Path(tmp) / "ssdA"
        ssd2 = Path(tmp) / "ssdB"
        (ssd1 / BATCHES_SUBDIR / "batch01").mkdir(parents=True)
        (ssd2 / BATCHES_SUBDIR / "batch01").mkdir(parents=True)
        one = a.choose_upload_sources(str(ssd1), "", "batch01")
        check("one SSD is allowed", len(one) == 1 and one[0].name == "batch01")
        raised = False
        try:
            a.choose_upload_sources(str(ssd1), str(ssd2), "batch01")
        except RuntimeError as exc:
            raised = True
            check("error tells operator to use Upload this SSD", "Upload this SSD" in str(exc), str(exc))
        check("two SSDs raise", raised)


def test_resolve_upload_ssds_one_slot() -> None:
    print("\n[4b] Upload this SSD does not fill in the other disk from config")
    cfg = {"ssd1": r"I:\\", "ssd2": r"F:\\"}
    one, two = a.resolve_upload_ssds({"ssd_slot": "1", "ssd1": r"I:\\", "ssd2": ""}, cfg)
    check("slot 1 keeps only SSD1", one == r"I:\\" and two == "", f"{one!r} {two!r}")
    one, two = a.resolve_upload_ssds({"ssd_slot": "2", "ssd1": "", "ssd2": r"F:\\"}, cfg)
    check("slot 2 keeps only SSD2", one == "" and two == r"F:\\", f"{one!r} {two!r}")
    one, two = a.resolve_upload_ssds({"ssd1": r"I:\\", "ssd2": ""}, cfg)
    check("one path in body does not pull SSD2 from config", two == "", two)


def test_plan_s3_dest_names() -> None:
    print("\n[5] S3-side -1 keeps Video A, remaps Video B")
    local_b = [
        ("GX010001.MP4", 200),
        ("GX010001.segments.json", 11),
    ]
    s3_a = {"GX010001.MP4": 100, "GX010001.segments.json": 10}
    mapping = a.plan_s3_dest_names(local_b, s3_a)
    check("MP4 remapped to -1", mapping["GX010001.MP4"] == "GX010001-1.MP4", str(mapping))
    check(
        "sidecar follows -1 stem",
        mapping["GX010001.segments.json"] == "GX010001-1.segments.json",
        str(mapping),
    )
    same = a.plan_s3_dest_names(
        [("GX010001.MP4", 100), ("GX010001.segments.json", 10)],
        s3_a,
    )
    check("same name + same size keeps name", same["GX010001.MP4"] == "GX010001.MP4")
    empty = a.plan_s3_dest_names([("GX010001.MP4", 100)], {})
    check("empty prefix keeps local name", empty["GX010001.MP4"] == "GX010001.MP4")


def test_compare_ignores_other_ssd_objects() -> None:
    print("\n[6] verify this job's keys only (extra SSD-A objects OK)")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "batch01"
        src.mkdir()
        (src / "GX010001.MP4").write_bytes(b"B" * 200)
        (src / "GX010001.segments.json").write_text("{}", encoding="utf-8")
        listed = {
            "GX010001.MP4": 100,
            "GX010001.segments.json": 2,
            "GX010001-1.MP4": 200,
            "GX010001-1.segments.json": (src / "GX010001.segments.json").stat().st_size,
        }
        key_map = {
            "GX010001.MP4": "GX010001-1.MP4",
            "GX010001.segments.json": "GX010001-1.segments.json",
        }
        with patch.object(a, "list_s3_object_sizes", return_value=listed):
            result = a._compare_local_s3_sizes([src], "s3://bucket/footage/batch01/", key_map=key_map)
        check("mapped keys match → ok", result["ok"] is True, str(result))
        prefix_only = {
            "GX010001.MP4": 100,
            "GX010001.segments.json": 2,
        }
        with patch.object(a, "list_s3_object_sizes", return_value=prefix_only):
            miss = a._compare_local_s3_sizes([src], "s3://bucket/footage/batch01/", key_map=key_map)
        check("missing mapped keys → fail", miss["ok"] is False)


def test_verify_timeout_and_mismatch_do_not_delete() -> None:
    print("\n[7] auto-delete only after successful verify")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "batch01"
        src.mkdir()
        body = b"keep-me"
        (src / "GX010001.MP4").write_bytes(body)
        job_id = "aws:tb-safety"
        base_job = {
            "id": job_id,
            "status": "completed",
            "verified": False,
            "auto_delete": True,
            "sources": [str(src)],
            "dest": "s3://bucket/footage/batch01/",
            "batch": "batch01",
            "key_map": {"GX010001.MP4": "GX010001.MP4"},
            "log": [],
        }
        with patch.object(a, "_persist_jobs"), patch.object(a, "list_s3_object_sizes", return_value=None):
            a._jobs[job_id] = dict(base_job)
            a._auto_verify_job(job_id)
            check("timeout did not delete folder", src.is_dir() and (src / "GX010001.MP4").is_file())
            live = a._jobs[job_id]
            check("timeout is not verified", live.get("verified") is False)
            check("timeout did not become mismatch", live.get("status") != "mismatch", str(live.get("status")))

        with patch.object(a, "_persist_jobs"), patch.object(
            a, "list_s3_object_sizes", return_value={"GX010001.MP4": 1}
        ):
            a._jobs[job_id] = dict(base_job)
            a._auto_verify_job(job_id)
            check("mismatch did not delete folder", src.is_dir() and (src / "GX010001.MP4").is_file())
            check("mismatch not verified", a._jobs[job_id].get("verified") is False)

        with patch.object(a, "_persist_jobs"), patch.object(
            a, "list_s3_object_sizes", return_value={"GX010001.MP4": len(body)}
        ), patch.object(a, "_on_batch_deleted", None):
            a._jobs[job_id] = dict(base_job)
            a._auto_verify_job(job_id)
            check("successful verify deleted this SSD folder", not src.exists())
            check("job marked deleted_local", a._jobs[job_id].get("status") == "deleted_local")
        a._jobs.pop(job_id, None)


def test_watchdog_stall_vs_progress() -> None:
    print("\n[8] watchdog: progressing copy kept; stalled live copy not killed")

    class Alive:
        def is_alive(self) -> bool:
            return True

    with tempfile.TemporaryDirectory() as tmp:
        engine._watchdog_copy_snap.clear()
        engine._cards.clear()
        engine._copy_threads.clear()
        engine._cards["C1"] = {
            "card_id": "C1",
            "status": "copying",
            "bytes_done": 50,
            "started_at": time.time() - 3600,
            "mount": tmp,
        }
        engine._copy_threads["C1"] = Alive()
        with patch.object(engine, "retry_card_job") as retry, patch.object(
            engine.aws_upload, "watchdog_pass", return_value=[]
        ), patch.object(engine, "_pump_closed_uploads"):
            engine.run_watchdog_once()
            check("first pass does not resume a live thread", retry.call_count == 0)
            snap = engine._watchdog_copy_snap.get("C1")
            check("snapshot stored", snap is not None and snap[0] == 50)

            engine._cards["C1"]["bytes_done"] = 50
            engine._watchdog_copy_snap["C1"] = (50, time.time() - engine.WATCHDOG_SECONDS - 5)
            engine.run_watchdog_once()
            check("stalled live copy is not killed/resumed", retry.call_count == 0)
            check("thread still treated as alive", engine._copy_threads["C1"].is_alive())
            msg = str(engine._cards["C1"].get("message") or "")
            check("stall message set", "stalled" in msg.lower(), msg)

            engine._cards["C1"]["bytes_done"] = 80
            engine._watchdog_copy_snap["C1"] = (50, time.time() - engine.WATCHDOG_SECONDS - 5)
            engine._cards["C1"]["message"] = ""
            engine.run_watchdog_once()
            check("progressing copy is not marked stalled", "stalled" not in str(engine._cards["C1"].get("message") or "").lower())
            check("progress updates snapshot bytes", engine._watchdog_copy_snap["C1"][0] == 80)
        engine._cards.clear()
        engine._copy_threads.clear()
        engine._watchdog_copy_snap.clear()


def test_interrupt_hotplug_and_watchdog_resume() -> None:
    print("\n[9] USB flicker does not auto-start; watchdog resumes interrupted")
    with tempfile.TemporaryDirectory() as tmp:
        engine._cards.clear()
        engine._copy_threads.clear()
        engine._waiting_queue.clear()
        engine._cancel_requested.clear()
        engine._retry_hold.clear()
        engine._cards["SD-1"] = {
            "card_id": "SD-1",
            "status": "interrupted",
            "mount": tmp,
            "message": "waiting for Retry",
        }
        engine._retry_hold.add("SD-1")
        engine._reconcile_hotplug({})
        check(
            "flicker keeps interrupted (not removed)",
            engine._cards["SD-1"]["status"] == "interrupted",
            str(engine._cards["SD-1"].get("status")),
        )
        with patch.object(engine, "retry_card_job") as retry, patch.object(
            engine.aws_upload, "watchdog_pass", return_value=[]
        ), patch.object(engine, "_pump_closed_uploads"):
            engine.run_watchdog_once()
            check("watchdog does not auto-retry interrupted cards", retry.call_count == 0)
        engine._cards.clear()
        engine._retry_hold.clear()


def test_live_copy_not_interrupted_on_usb_flicker() -> None:
    print("\n[11] live slow copy stays COPYING when Windows misses the drive for a tick")

    class Alive:
        def is_alive(self) -> bool:
            return True

    engine._cards.clear()
    engine._copy_threads.clear()
    engine._retry_hold.clear()
    engine._cancel_requested.clear()
    engine._cards["SD-1"] = {
        "card_id": "SD-1",
        "status": "copying",
        "mount": "X:\\",
        "message": "Copying GX01.MP4",
        "bytes_done": 20_000_000_000,
    }
    engine._copy_threads["SD-1"] = Alive()
    engine._reconcile_hotplug({})
    check(
        "status stays copying",
        engine._cards["SD-1"]["status"] == "copying",
        str(engine._cards["SD-1"].get("status")),
    )
    check("not on retry hold", "SD-1" not in engine._retry_hold)
    check("worker not cancelled", "SD-1" not in engine._cancel_requested)
    engine._mark_waiting_for_retry("SD-1", message="should ignore")
    check(
        "mark retry ignored while thread alive",
        engine._cards["SD-1"]["status"] == "copying",
        str(engine._cards["SD-1"].get("status")),
    )
    engine._cards.clear()
    engine._copy_threads.clear()
    engine._retry_hold.clear()


def test_missing_auto_delete_still_deletes() -> None:
    print("\n[10] verified jobs delete even if auto_delete key was missing")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "batch01"
        src.mkdir()
        body = b"keep-me"
        (src / "GX010001.MP4").write_bytes(body)
        job_id = "aws:missing-flag"
        job = {
            "id": job_id,
            "status": "completed",
            "verified": False,
            "sources": [str(src)],
            "dest": "s3://bucket/footage/batch01/",
            "batch": "batch01",
            "key_map": {"GX010001.MP4": "GX010001.MP4"},
            "log": [],
        }
        with patch.object(a, "_persist_jobs"), patch.object(
            a, "list_s3_object_sizes", return_value={"GX010001.MP4": len(body)}
        ), patch.object(a, "_on_batch_deleted", None):
            a._jobs[job_id] = dict(job)
            a._auto_verify_job(job_id)
            check("missing auto_delete still deleted folder", not src.exists())
            check("job marked deleted_local", a._jobs[job_id].get("status") == "deleted_local")

        src.mkdir()
        (src / "GX010001.MP4").write_bytes(body)
        job["auto_delete"] = False
        with patch.object(a, "_persist_jobs"), patch.object(
            a, "list_s3_object_sizes", return_value={"GX010001.MP4": len(body)}
        ), patch.object(a, "_on_batch_deleted", None):
            a._jobs[job_id] = dict(job)
            a._auto_verify_job(job_id)
            check("explicit auto_delete False keeps folder", src.exists())
            check("still verified", a._jobs[job_id].get("verified") is True)
        a._jobs.pop(job_id, None)


def test_s3_list_empty_prefix_is_not_error() -> None:
    print("\n[9] empty S3 prefix is allowed; access denied is not")

    class _Proc:
        def __init__(self, text: str, returncode: int = 1):
            self.stdout = ""
            self.stderr = text
            self.returncode = returncode

    dest = "s3://bucket/footage/Batch-29/"

    def empty_ls(cmd, **_kwargs):
        return _Proc('ERROR "s3://bucket/footage/Batch-29/": no object found')

    with patch.object(a, "s5cmd_available", return_value=True), patch.object(
        a, "aws_cli_available", return_value=False
    ), patch.object(a.subprocess, "run", side_effect=empty_ls):
        listed = a.list_s3_object_sizes(dest)
    check("s5cmd no object found → empty dict", listed == {}, str(listed))

    def denied_ls(cmd, **_kwargs):
        return _Proc("AccessDenied: not allowed to list", 1)

    with patch.object(a, "s5cmd_available", return_value=True), patch.object(
        a, "aws_cli_available", return_value=False
    ), patch.object(a.subprocess, "run", side_effect=denied_ls):
        listed = a.list_s3_object_sizes(dest)
    check("access denied → refuse listing", listed is None)


def test_cancel_job_is_immediate_and_blocks_reattach() -> None:
    print("\n[12] AWS cancel drops live state immediately")
    job_id = "aws:cancel-now"
    dest = "s3://bucket/worldcontext-data/raw/batches/Batch-25/"
    a._jobs[job_id] = {
        "id": job_id,
        "status": "running",
        "batch": "Batch-25",
        "dest": dest,
        "speed_mbps": 82.5,
        "bytes_done": 5_000_000_000,
        "aws_pid": 4242,
        "log": [],
        "cancel_requested": False,
    }
    with patch.object(a, "_persist_jobs"), patch.object(a.threading, "Thread") as th:
        out = a.cancel_job(job_id)
        check("status is cancelled now", out.get("status") == "cancelled", str(out.get("status")))
        check("speed is 0", out.get("speed_mbps") == 0, str(out.get("speed_mbps")))
        check("cancel_requested set", bool(out.get("cancel_requested")))
        check("pid cleared", out.get("aws_pid") is None)
        check("remembered pid", out.get("cancelled_pid") == 4242)
        check("kill dispatched", th.called)
        check(
            "same dest cannot re-attach",
            a._cancelled_blocks_reattach_locked(pid=4242, dest=dest, batch="Batch-25"),
        )
        check(
            "trailing-slash dest still matches",
            a._cancelled_blocks_reattach_locked(dest=dest.rstrip("/"), batch="Batch-25"),
        )
        again = a.cancel_job(job_id)
        check("second cancel allowed", again.get("status") == "cancelled")

        a._jobs[job_id]["status"] = "cancelling"
        a._jobs[job_id]["speed_mbps"] = 40
        a._finalize_stuck_cancels()
        check(
            "stuck cancelling becomes cancelled",
            a._jobs[job_id]["status"] == "cancelled",
            str(a._jobs[job_id].get("status")),
        )
        check("stuck speed zeroed", a._jobs[job_id].get("speed_mbps") == 0)

        with patch.object(a, "restart_job") as restart:
            notes = a.watchdog_pass()
            check("watchdog does not resume cancelled", restart.call_count == 0, str(notes))
    a._jobs.pop(job_id, None)


def main() -> int:
    test_timeout_constant()
    test_size_slack()
    test_dir_bytes_manifest()
    test_choose_upload_sources()
    test_resolve_upload_ssds_one_slot()
    test_plan_s3_dest_names()
    test_compare_ignores_other_ssd_objects()
    test_verify_timeout_and_mismatch_do_not_delete()
    test_watchdog_stall_vs_progress()
    test_interrupt_hotplug_and_watchdog_resume()
    test_live_copy_not_interrupted_on_usb_flicker()
    test_missing_auto_delete_still_deletes()
    test_s3_list_empty_prefix_is_not_error()
    test_cancel_job_is_immediate_and_blocks_reattach()
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())

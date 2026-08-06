"""Tests for annotation sidecars, CSV batches, and reports."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_cleaner.core import annotation_store, batch_registry, reporting


class AnnotationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "GX010001.MP4"
        self.video.write_bytes(b"fake")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_contiguous_work_and_garbage(self) -> None:
        with patch("gopro_cleaner.core.annotation_store.probe_media", create=True):
            pass
        result = annotation_store.save_annotation(
            self.video,
            {
                "duration": 100.0,
                "batch_name": "batch-1",
                "factory": "Factory A",
                "card_badge": "C1234",
                "device_type": "gopro",
                "device_id": "GP-01",
                "segments": [
                    {"kind": "work", "start": 0, "end": 30, "task": "Stitching"},
                    {"kind": "garbage", "start": 30, "end": 70},
                    {"kind": "work", "start": 70, "end": 100, "task": "Packing"},
                ],
            },
            require_complete=True,
        )
        self.assertTrue(result["summary"]["complete"])
        self.assertAlmostEqual(result["summary"]["work_seconds"], 60.0)
        self.assertAlmostEqual(result["summary"]["garbage_seconds"], 40.0)
        loaded = annotation_store.load_annotation(self.video)
        self.assertEqual(loaded["card_badge"], "C1234")
        self.assertTrue((self.root / "GX010001.segments.json").is_file())
        self.assertTrue((self.root / "GX010001.segments.txt").is_file())

    def test_rejects_gap(self) -> None:
        with self.assertRaises(ValueError):
            annotation_store.save_annotation(
                self.video,
                {
                    "duration": 50.0,
                    "segments": [
                        {"kind": "work", "start": 0, "end": 10, "task": "A"},
                        {"kind": "work", "start": 15, "end": 20, "task": "B"},
                    ],
                },
            )

    def test_append_and_undo(self) -> None:
        annotation_store.save_annotation(
            self.video,
            {"duration": 40.0, "segments": []},
        )
        annotation_store.append_segment(
            self.video,
            kind="work",
            end=12.0,
            task="Stitching",
            context={"duration": 40.0, "batch_name": "b1"},
        )
        annotation_store.append_segment(
            self.video,
            kind="garbage",
            end=40.0,
            context={"duration": 40.0},
        )
        summary = annotation_store.coverage_summary(annotation_store.load_annotation(self.video))
        self.assertTrue(summary["complete"])
        annotation_store.undo_last_segment(self.video)
        summary2 = annotation_store.coverage_summary(annotation_store.load_annotation(self.video))
        self.assertFalse(summary2["complete"])
        self.assertAlmostEqual(summary2["covered_seconds"], 12.0)

    def test_near_end_normalization(self) -> None:
        self.assertEqual(annotation_store.normalize_boundary(99.97, 100.0), 100.0)
        self.assertEqual(annotation_store.normalize_boundary(50.0, 100.0), 50.0)


class BatchRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self._patch = patch.object(batch_registry, "STATE_DIR", self.state)
        self._patch.start()
        batch_registry.ensure_state_dir()

    def tearDown(self) -> None:
        self._patch.stop()
        self.tmp.cleanup()

    def test_csv_import_and_report(self) -> None:
        csv_text = (
            "batch_name,factory,card_badge,device_type,device_id\n"
            "batch-1,Factory A,C1001,gopro,GP-01\n"
            "batch-1,Factory A,C1002,stereo,ST-01\n"
        )
        detail = batch_registry.create_batch_from_csv(csv_text)
        self.assertEqual(detail["batch_name"], "batch-1")
        self.assertEqual(detail["card_count"], 2)
        self.assertEqual(detail["status"], "open")

        video_dir = self.state / "card"
        video_dir.mkdir()
        video = video_dir / "GX01.MP4"
        video.write_bytes(b"x")
        annotation_store.save_annotation(
            video,
            {
                "duration": 3600.0,
                "segments": [
                    {"kind": "work", "start": 0, "end": 1800, "task": "Stitching"},
                    {"kind": "garbage", "start": 1800, "end": 3600},
                ],
            },
            require_complete=True,
        )
        batch_registry.bind_card(
            detail["id"],
            card_badge="C1001",
            mount_path=str(video_dir),
            scan_path=str(video_dir),
            videos=[{"path": str(video), "name": "GX01.MP4", "duration": 3600.0}],
        )
        synced = batch_registry.sync_asset_annotations(detail["id"])
        report = synced["report"]
        self.assertAlmostEqual(report["totals"]["raw_hours"], 1.0)
        self.assertAlmostEqual(report["totals"]["clean_hours"], 0.5)
        self.assertAlmostEqual(report["totals"]["garbage_hours"], 0.5)
        self.assertEqual(report["totals"]["task_count"], 1)
        self.assertTrue(any("C1002" in b for b in report["blocking"]))

        # Finish only works when complete and all assets ready — C1001 should finish
        finished = batch_registry.finish_card(detail["id"], "C1001")
        card = next(c for c in finished["cards"] if c["card_badge"] == "C1001")
        self.assertEqual(card["status"], "complete")

        with self.assertRaises(ValueError):
            batch_registry.complete_batch(detail["id"])

    def test_csv_requires_single_batch(self) -> None:
        csv_text = (
            "batch_name,factory,card_badge,device_type,device_id\n"
            "batch-1,Factory,C1001,gopro,1\n"
            "batch-2,Factory,C1002,gopro,2\n"
        )
        with self.assertRaises(ValueError):
            batch_registry.parse_batch_csv(csv_text)

    def test_remove_discarded_asset(self) -> None:
        detail = batch_registry.create_batch_from_csv(
            "batch_name,factory,card_badge,device_type,device_id\n"
            "batch-delete,Factory,C1001,gopro,GP-01\n"
        )
        video = self.state / "short.MP4"
        video.write_bytes(b"x")
        batch_registry.bind_card(
            detail["id"],
            card_badge="C1001",
            mount_path=str(self.state),
            scan_path=str(self.state),
            videos=[{"path": str(video), "name": video.name, "duration": 3.0}],
        )

        updated = batch_registry.remove_asset(detail["id"], str(video))
        card = next(c for c in updated["cards"] if c["card_badge"] == "C1001")
        self.assertEqual(card["assets"], [])

    def test_report_csv_export(self) -> None:
        detail = batch_registry.create_batch_from_csv(
            "batch_name,factory,card_badge,device_type,device_id\n"
            "batch-9,Fac,C9999,gopro,GP\n"
        )
        data = batch_registry.get_batch(detail["id"])
        text = batch_registry.report_csv(data)
        self.assertIn("raw_hours", text)
        self.assertIn("C9999", text)

    def test_reporting_rows_are_sheet_ready(self) -> None:
        detail = batch_registry.create_batch_from_csv(
            "batch_name,factory,card_badge,device_type,device_id\n"
            "batch-rows,Factory A,C3001,gopro,GP-03\n"
        )
        rows = reporting.build_report_rows(detail)
        self.assertTrue(rows)
        self.assertIn("batch_name", rows[0])
        self.assertEqual(rows[0]["card_badge"], "C3001")
        self.assertEqual(rows[0]["factory"], "Factory A")

    def test_daily_process_tracks_card_lifecycle(self) -> None:
        card_dir = self.state / "daily-card"
        dcim = card_dir / "DCIM" / "100GOPRO"
        dcim.mkdir(parents=True)
        (dcim / "GX010001.MP4").write_bytes(b"x")
        (dcim / "GX010002.MP4").write_bytes(b"x")

        for video in [card_dir / "DCIM" / "100GOPRO" / "GX010001.MP4", card_dir / "DCIM" / "100GOPRO" / "GX010002.MP4"]:
            annotation_store.save_annotation(
                video,
                {
                    "duration": 10.0,
                    "segments": [{"kind": "work", "start": 0.0, "end": 10.0, "task": "Review"}],
                },
                require_complete=True,
            )

        process = reporting.start_process(date="2026-08-03", employee="Ali")
        row = reporting.add_card_to_process(
            process["id"],
            card_path=str(card_dir),
            card_name="C3001",
        )

        self.assertEqual(row["status"], "Pending")
        self.assertEqual(row["card_name"], "C3001")
        self.assertEqual(row["total_mp4_videos"], 2)
        self.assertAlmostEqual(row["original_duration"], 20.0)
        self.assertAlmostEqual(row["used_space_before_labeling_gb"], 0.0, places=6)

        completed = reporting.finish_card(
            process["id"],
            row["id"],
            final_duration=12.0,
            used_space_after_labeling_gb=3.5,
        )
        self.assertEqual(completed["status"], "Completed")
        self.assertAlmostEqual(completed["final_duration"], 12.0)
        self.assertAlmostEqual(completed["duration_difference"], 8.0)
        self.assertAlmostEqual(completed["original_duration_after_labeling"], 12.0)
        self.assertAlmostEqual(completed["used_space_after_labeling_gb"], 3.5)


if __name__ == "__main__":
    unittest.main()

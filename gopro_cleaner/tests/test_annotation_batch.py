"""Tests for annotation sidecars, CSV batches, and reports."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_cleaner.core import annotation_store, batch_registry


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

    def test_report_csv_export(self) -> None:
        detail = batch_registry.create_batch_from_csv(
            "batch_name,factory,card_badge,device_type,device_id\n"
            "batch-9,Fac,C9999,gopro,GP\n"
        )
        data = batch_registry.get_batch(detail["id"])
        text = batch_registry.report_csv(data)
        self.assertIn("raw_hours", text)
        self.assertIn("C9999", text)


if __name__ == "__main__":
    unittest.main()

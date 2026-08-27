"""Tests for the two-layer ScaleAI sidecar."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_cleaner.core import scaleai_store


class ScaleAIStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task_dir = (
            self.root / "50 hours" / "AWS" / "Label Attachment" / "Label Attachment"
        )
        self.task_dir.mkdir(parents=True)
        self.video = self.task_dir / "GX010001.MP4"
        self.video.write_bytes(b"fake")
        self.gdrive_dir = (
            self.root
            / "50 hours"
            / "Google Drive"
            / "Axle Shaft Cutting"
            / "Axle Shaft Cutting"
        )
        self.gdrive_dir.mkdir(parents=True)
        self.gdrive_video = self.gdrive_dir / "clp_demo.mp4"
        self.gdrive_video.write_bytes(b"fake")
        self.duration_patch = patch(
            "gopro_cleaner.core.scaleai_store.resolve_media_duration",
            return_value=600.0,
        )
        self.duration_patch.start()

    def tearDown(self) -> None:
        self.duration_patch.stop()
        self.tmp.cleanup()

    def test_infers_task_from_aws_folder(self) -> None:
        self.assertEqual(
            scaleai_store.infer_parent_task(self.video),
            "Label Attachment",
        )

    def test_infers_task_from_nested_google_drive_folder(self) -> None:
        self.assertEqual(
            scaleai_store.infer_parent_task(self.gdrive_video),
            "Axle Shaft Cutting",
        )

    def test_infers_task_when_scan_root_is_50_hours(self) -> None:
        fifty = self.root / "50 hours"
        self.assertEqual(
            scaleai_store.infer_parent_task(self.gdrive_video, root=fifty),
            "Axle Shaft Cutting",
        )

    def test_parent_and_subtask_layers_overlap_safely(self) -> None:
        payload = scaleai_store.add_parent_cycle(self.video, 5.0, 12.0)
        cycle_id = payload["parent_cycles"][0]["id"]
        payload = scaleai_store.add_subtask_segment(
            self.video, cycle_id, "grab-cloth", 5.1, 5.5
        )
        payload = scaleai_store.add_subtask_segment(
            self.video, cycle_id, "place-label", 6.0, 7.0
        )
        self.assertEqual(payload["parent_task"], "Label Attachment")
        self.assertEqual(len(payload["parent_cycles"]), 1)
        self.assertEqual(len(payload["subtask_segments"]), 2)
        self.assertEqual(payload["subtask_names"], ["grab-cloth", "place-label"])

    def test_rejects_subtask_outside_parent_cycle(self) -> None:
        payload = scaleai_store.add_parent_cycle(self.video, 5.0, 12.0)
        cycle_id = payload["parent_cycles"][0]["id"]
        with self.assertRaisesRegex(ValueError, "inside parent cycle"):
            scaleai_store.add_subtask_segment(
                self.video, cycle_id, "grab-cloth", 4.0, 5.4
            )

    def test_select_example_and_delete_cycle_cascades(self) -> None:
        payload = scaleai_store.add_parent_cycle(self.video, 5.0, 12.0)
        cycle_id = payload["parent_cycles"][0]["id"]
        scaleai_store.add_subtask_segment(
            self.video, cycle_id, "grab-cloth", 5.1, 5.5
        )
        payload = scaleai_store.select_example(self.video, cycle_id)
        self.assertEqual(payload["example_cycle_id"], cycle_id)
        payload = scaleai_store.delete_parent_cycle(self.video, cycle_id)
        self.assertEqual(payload["parent_cycles"], [])
        self.assertEqual(payload["subtask_segments"], [])
        self.assertIsNone(payload["example_cycle_id"])

    def test_rejects_overlapping_parent_cycles(self) -> None:
        scaleai_store.add_parent_cycle(self.video, 5.0, 12.0)
        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            scaleai_store.add_parent_cycle(self.video, 10.0, 14.0)

    def test_parent_task_has_only_one_example_across_videos(self) -> None:
        other = self.task_dir / "GX010002.MP4"
        other.write_bytes(b"fake")
        first = scaleai_store.add_parent_cycle(self.video, 5.0, 12.0)
        scaleai_store.select_example(self.video, first["parent_cycles"][0]["id"])

        second = scaleai_store.add_parent_cycle(other, 20.0, 24.0)
        second_id = second["parent_cycles"][0]["id"]
        scaleai_store.select_example(other, second_id)

        first_reloaded = scaleai_store.load_annotation(self.video)
        second_reloaded = scaleai_store.load_annotation(other)
        self.assertIsNone(first_reloaded["example_cycle_id"])
        self.assertEqual(second_reloaded["example_cycle_id"], second_id)
        self.assertEqual(first_reloaded["parent_example"]["source"], str(other.resolve()))


if __name__ == "__main__":
    unittest.main()

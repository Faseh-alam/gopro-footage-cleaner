"""Tests for Scale AI 50-hour free-form subtask labeling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gopro_cleaner.core import fifty_hour_store
from gopro_cleaner.core.eager import is_scaleai_source_footage, scan_mp4_files


class FiftyHourStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "50-hour"
        self.task = self.root / "garment-folding-general"
        self.task.mkdir(parents=True)
        self.video = self.task / "GX010001.MP4"
        self.video.write_bytes(b"fake-mp4")

    def test_parent_task_from_root_child(self) -> None:
        self.assertEqual(
            fifty_hour_store.infer_parent_task(self.video, self.root),
            "garment-folding-general",
        )

    def test_sidecar_is_task_level_segment_json(self) -> None:
        path = fifty_hour_store.sidecar_path_for(self.video)
        self.assertEqual(path, self.task / "segment.json")

    def test_add_subtask_and_garbage(self) -> None:
        ann = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.5,
            label="pick-cloth",
            segment_type="subtask",
            root=self.root,
        )
        ann = fifty_hour_store.add_segment(
            self.video,
            start=2.5,
            end=4.0,
            label="garbage",
            segment_type="garbage",
            root=self.root,
        )
        self.assertEqual(len(ann["segments"]), 2)
        self.assertEqual(fifty_hour_store.usable_seconds(ann), 1.5)
        labels = fifty_hour_store.labels_for_task(self.root, "garment-folding-general")
        self.assertIn("pick-cloth", labels)
        progress = fifty_hour_store.refresh_progress(self.root)
        row = progress["tasks"][0]
        self.assertAlmostEqual(row["labeled_hours"], 1.5 / 3600.0, places=4)
        self.assertFalse(row["complete"])

    def test_overlap_auto_bumps_start_by_10ms(self) -> None:
        fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="a",
            segment_type="subtask",
            root=self.root,
        )
        ann = fifty_hour_store.add_segment(
            self.video,
            start=2.0,
            end=3.0,
            label="b",
            segment_type="subtask",
            root=self.root,
        )
        second = next(s for s in ann["segments"] if s["label"] == "b")
        self.assertAlmostEqual(second["start"], 2.01, places=3)
        self.assertAlmostEqual(second["end"], 3.0, places=3)

    def test_export_paths_and_manifest(self) -> None:
        ann = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.5,
            label="pick cloth",
            segment_type="subtask",
            root=self.root,
        )
        ann["camera_serial"] = "CAM001"
        fifty_hour_store.save_annotation(self.video, ann, root=self.root)
        export_dir = fifty_hour_store.export_directory(self.video)
        sub = fifty_hour_store.subtask_export_directory(self.video, "pick cloth")
        self.assertEqual(export_dir, self.task)
        self.assertEqual(sub, self.task / "pick-cloth-001")
        sub.mkdir(parents=True, exist_ok=True)
        name = fifty_hour_store.next_clip_filename(self.video, "pick cloth", sub)
        self.assertEqual(name, "CAM001-001-001.mp4")
        (sub / name).write_bytes(b"clip")
        name2 = fifty_hour_store.next_clip_filename(self.video, "pick cloth", sub)
        self.assertEqual(name2, "CAM001-001-002.mp4")
        reserved = {"CAM001-001-001.mp4", "CAM001-001-002.mp4"}
        name3 = fifty_hour_store.next_clip_filename(
            self.video, "pick cloth", sub, reserved=reserved
        )
        self.assertEqual(name3, "CAM001-001-003.mp4")
        manifest = fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": name,
                    "source_video": "GX010001.MP4",
                    "parent_task": "garment-folding-general",
                    "subtask": "pick cloth",
                    "source_start": "1.000",
                    "source_end": "2.500",
                    "duration": "1.500",
                    "camera_serial": "",
                    "cl_number": "",
                }
            ],
        )
        self.assertTrue(manifest.is_file())
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest.name, "manifest.json")
        self.assertEqual(manifest_data["subtasks"][0]["id"], "001")
        self.assertEqual(manifest_data["subtasks"][0]["total_clips"], 1)
        self.assertEqual(manifest_data["subtasks"][0]["clips"][0]["filename"], name)
        self.assertTrue(fifty_hour_store.is_export_path(sub / name, self.root))

    def test_scan_skips_generated_clip_in_subtask_folder(self) -> None:
        clip = self.task / "pick-cloth-001" / "CAM001-001-001.mp4"
        clip.parent.mkdir()
        clip.write_bytes(b"clip")
        self.assertTrue(is_scaleai_source_footage(self.video, root=self.root))
        self.assertFalse(is_scaleai_source_footage(clip, root=self.root))

    def test_first_video_defines_subtasks_for_later_videos(self) -> None:
        second = self.task / "GX010002.MP4"
        second.write_bytes(b"fake-mp4-2")
        self.assertEqual(
            fifty_hour_store.labels_for_task(self.root, "garment-folding-general"),
            [],
        )
        fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="pick up box",
            root=self.root,
        )
        self.assertEqual(
            fifty_hour_store.labels_for_task(self.root, "garment-folding-general"),
            ["pick up box"],
        )
        fifty_hour_store.add_segment(
            second,
            start=3.0,
            end=4.0,
            label="pick up box",
            root=self.root,
        )
        segment_doc = json.loads(
            (self.task / "segment.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [row["source_video"] for row in segment_doc["videos"]],
            ["GX010001.MP4", "GX010002.MP4"],
        )

    def test_old_video_json_is_migrated_before_trim(self) -> None:
        legacy = self.video.with_name("GX010001.json")
        legacy.write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_video": "GX010001.MP4",
                    "source_path": "/old/computer/GX010001.MP4",
                    "parent_task": "garment-folding-general",
                    "camera_serial": "CAM001",
                    "duration_seconds": 10.0,
                    "segments": [
                        {
                            "id": 1,
                            "start": 1.0,
                            "end": 2.0,
                            "type": "subtask",
                            "label": "pick cloth",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        annotation = fifty_hour_store.load_annotation(self.video, root=self.root)

        self.assertEqual(annotation["source_path"], str(self.video.resolve()))
        self.assertTrue((self.task / "segment.json").is_file())
        manifest = json.loads((self.task / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [(row["id"], row["name"]) for row in manifest["subtasks"]],
            [("001", "pick cloth")],
        )
        self.assertEqual(
            fifty_hour_store.next_clip_filename(
                self.video,
                "pick cloth",
                fifty_hour_store.subtask_export_directory(self.video, "pick cloth"),
            ),
            "CAM001-001-001.mp4",
        )

    def test_direct_source_clips_move_to_subtask_folder_with_camera_name(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="pick cloth",
            root=self.root,
        )
        annotation["camera_serial"] = "CAM001"
        annotation["segments"][0]["clip_filename"] = "GX010001.001.001.mp4"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        old_clip = self.task / "GX010001.001.001.mp4"
        old_clip.write_bytes(b"clip")
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": old_clip.name,
                    "source_video": self.video.name,
                    "subtask": "pick cloth",
                    "camera_serial": "CAM001",
                }
            ],
        )

        manifest = fifty_hour_store.load_manifest(self.task)

        new_clip = self.task / "pick-cloth-001" / "CAM001-001-001.mp4"
        self.assertTrue(new_clip.is_file())
        self.assertFalse(old_clip.exists())
        self.assertEqual(manifest["subtasks"][0]["clips"][0]["filename"], new_clip.name)
        self.assertEqual(manifest["subtasks"][0]["clips"][0]["camera_serial"], "CAM001")
        segment_doc = json.loads((self.task / "segment.json").read_text(encoding="utf-8"))
        self.assertEqual(
            segment_doc["videos"][0]["segments"][0]["clip_filename"],
            new_clip.name,
        )


class FiftyHourScanSortTests(unittest.TestCase):
    def test_scan_groups_by_parent_task(self) -> None:
        root = Path(tempfile.mkdtemp()) / "50-hour"
        a = root / "aaa-task"
        b = root / "bbb-task"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        (a / "Z.MP4").write_bytes(b"a")
        (b / "A.MP4").write_bytes(b"b")
        # Avoid ffmpeg requirement by testing ordering helper indirectly via store.
        paths = sorted(
            [a / "Z.MP4", b / "A.MP4"],
            key=lambda p: (
                fifty_hour_store.infer_parent_task(p, root).lower(),
                p.name.lower(),
            ),
        )
        self.assertEqual([p.parent.name for p in paths], ["aaa-task", "bbb-task"])


if __name__ == "__main__":
    unittest.main()

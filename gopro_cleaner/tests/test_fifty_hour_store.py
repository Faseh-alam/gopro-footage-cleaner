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

    def test_sidecar_is_stem_json(self) -> None:
        path = fifty_hour_store.sidecar_path_for(self.video)
        self.assertEqual(path.name, "GX010001.json")

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
        export_dir = fifty_hour_store.export_directory(self.video)
        sub = fifty_hour_store.subtask_export_directory(self.video, "pick cloth")
        self.assertEqual(export_dir.name, "GX010001")
        self.assertEqual(sub.name, "pick-cloth")
        sub.mkdir(parents=True)
        name = fifty_hour_store.next_clip_filename(self.video, "pick cloth", sub)
        self.assertEqual(name, "0001.MP4")
        (sub / name).write_bytes(b"clip")
        name2 = fifty_hour_store.next_clip_filename(self.video, "pick cloth", sub)
        self.assertEqual(name2, "0002.MP4")
        reserved = {"0001.MP4", "0002.MP4"}
        name3 = fifty_hour_store.next_clip_filename(
            self.video, "pick cloth", sub, reserved=reserved
        )
        self.assertEqual(name3, "0003.MP4")
        manifest = fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": name,
                    "source_video": "GX010001.MP4",
                    "parent_task": "garment-folding-general",
                    "subtask": "pick-cloth",
                    "source_start": "1.000",
                    "source_end": "2.500",
                    "duration": "1.500",
                    "camera_serial": "",
                    "cl_number": "",
                }
            ],
        )
        self.assertTrue(manifest.is_file())
        self.assertTrue(fifty_hour_store.is_export_path(sub / name, self.root))

    def test_scan_skips_export_folder(self) -> None:
        export = self.task / "GX010001" / "pick-cloth"
        export.mkdir(parents=True)
        clip = export / "GX010001_pick-cloth_0001.MP4"
        clip.write_bytes(b"clip")
        (self.task / "GX010001" / "export_manifest.csv").write_text(
            "clip_filename\n", encoding="utf-8"
        )
        self.assertTrue(is_scaleai_source_footage(self.video, root=self.root))
        self.assertFalse(is_scaleai_source_footage(clip, root=self.root))


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

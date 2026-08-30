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

    def test_sidecar_is_per_video_json(self) -> None:
        path = fifty_hour_store.sidecar_path_for(self.video)
        self.assertEqual(path, self.task / "GX010001.json")

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
        self.assertAlmostEqual(manifest_data["subtasks"][0]["duration_seconds"], 1.5)
        self.assertAlmostEqual(manifest_data["total_duration_seconds"], 1.5)
        self.assertEqual(manifest_data["subtasks"][0]["clips"][0]["filename"], name)
        self.assertAlmostEqual(
            manifest_data["subtasks"][0]["clips"][0]["duration_seconds"], 1.5
        )
        self.assertTrue(fifty_hour_store.is_export_path(sub / name, self.root))

    def test_manifest_records_subtask_and_stitched_duration(self) -> None:
        fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.5,
            label="pick cloth",
            root=self.root,
        )
        fifty_hour_store.add_segment(
            self.video,
            start=3.0,
            end=4.0,
            label="pick cloth",
            root=self.root,
        )
        data = fifty_hour_store.load_manifest(self.task)
        self.assertAlmostEqual(data["subtasks"][0]["duration_seconds"], 2.5)
        self.assertAlmostEqual(data["total_duration_seconds"], 2.5)
        folder = data["subtasks"][0]["folder"]
        stitched = self.task / folder / f"{folder}-stitched.mp4"
        stitched.parent.mkdir(parents=True, exist_ok=True)
        stitched.write_bytes(b"stitched")
        updated = fifty_hour_store.update_stitch_durations(
            self.video,
            [
                {
                    "ok": True,
                    "task": "pick cloth",
                    "output": str(stitched),
                    "duration": 2.4,
                }
            ],
        )
        self.assertEqual(updated["subtasks"][0]["stitched_filename"], stitched.name)
        self.assertAlmostEqual(updated["subtasks"][0]["stitched_duration_seconds"], 2.4)
        self.assertAlmostEqual(updated["total_stitched_duration_seconds"], 2.4)

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
        first = json.loads((self.task / "GX010001.json").read_text(encoding="utf-8"))
        second_doc = json.loads((self.task / "GX010002.json").read_text(encoding="utf-8"))
        self.assertEqual(first["source_video"], "GX010001.MP4")
        self.assertEqual(second_doc["source_video"], "GX010002.MP4")
        self.assertFalse((self.task / "segment.json").exists())
        self.assertTrue((self.task / "manifest.json").is_file())

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
        self.assertTrue((self.task / "GX010001.json").is_file())
        self.assertFalse((self.task / "segment.json").exists())
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
        video_doc = json.loads((self.task / "GX010001.json").read_text(encoding="utf-8"))
        self.assertEqual(
            video_doc["segments"][0]["clip_filename"],
            new_clip.name,
        )

    def test_legacy_segment_json_splits_and_junk_sidecars_are_removed(self) -> None:
        second = self.task / "GX010002.MP4"
        second.write_bytes(b"fake-mp4-2")
        (self.task / "segment.json").write_text(
            json.dumps(
                {
                    "version": 2,
                    "main_task": "garment-folding-general",
                    "videos": [
                        {
                            "source_video": "GX010001.MP4",
                            "source_path": str(self.video),
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
                        },
                        {
                            "source_video": "GX010002.MP4",
                            "source_path": str(second),
                            "parent_task": "garment-folding-general",
                            "duration_seconds": 10.0,
                            "segments": [],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.task / "GX010001.segments.txt").write_text("old\n", encoding="utf-8")
        (self.task / "GX010001.segments.json").write_text("{}", encoding="utf-8")
        (self.task / "GX010001.scaleai.json").write_text("{}", encoding="utf-8")
        junk_dir = self.task / "pick-cloth-001"
        junk_dir.mkdir()
        (junk_dir / "CAM001-001-001.scaleai-source.json").write_text("{}", encoding="utf-8")
        (junk_dir / "pick-cloth-001-stitched.manifest.json").write_text("{}", encoding="utf-8")

        fifty_hour_store.load_annotation(self.video, root=self.root)

        self.assertFalse((self.task / "segment.json").exists())
        self.assertTrue((self.task / "GX010001.json").is_file())
        self.assertTrue((self.task / "GX010002.json").is_file())
        first = json.loads((self.task / "GX010001.json").read_text(encoding="utf-8"))
        self.assertEqual(first["segments"][0]["label"], "pick cloth")
        self.assertFalse((self.task / "GX010001.segments.txt").exists())
        self.assertFalse((self.task / "GX010001.segments.json").exists())
        self.assertFalse((self.task / "GX010001.scaleai.json").exists())
        self.assertFalse((junk_dir / "CAM001-001-001.scaleai-source.json").exists())
        self.assertFalse((junk_dir / "pick-cloth-001-stitched.manifest.json").exists())
        self.assertTrue((self.task / "manifest.json").is_file())

    def test_segments_json_promotes_when_no_segment_json(self) -> None:
        (self.task / "GX010001.segments.json").write_text(
            json.dumps(
                {
                    "source": "GX010001.MP4",
                    "duration": 10.0,
                    "segments": [
                        {"start": 1.0, "end": 2.0, "kind": "work", "task": "pick cloth"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.task / "GX010001.segments.txt").write_text("old\n", encoding="utf-8")
        fifty_hour_store.cleanup_task_folder_files(self.task, root=self.root)
        self.assertTrue((self.task / "GX010001.json").is_file())
        self.assertFalse((self.task / "GX010001.segments.json").exists())
        self.assertFalse((self.task / "GX010001.segments.txt").exists())
        data = json.loads((self.task / "GX010001.json").read_text(encoding="utf-8"))
        self.assertEqual(data["source_video"], "GX010001.MP4")
        self.assertEqual(data["segments"][0]["label"], "pick cloth")

    def test_retarget_clip_filename_keeps_camera_and_serial(self) -> None:
        self.assertEqual(
            fifty_hour_store.retarget_clip_filename(
                "C3461325829225-001-053.mp4", "003"
            ),
            "C3461325829225-003-053.mp4",
        )
        self.assertEqual(
            fifty_hour_store.retarget_clip_filename(
                "C3461325829225-001-053.mp4",
                "003",
                occupied={"C3461325829225-003-053.mp4"},
            ),
            "C3461325829225-003-054.mp4",
        )
        self.assertEqual(
            fifty_hour_store.retarget_clip_filename(
                "C3531325142202-001-001.mp4",
                "003",
                occupied={"C3461325829225-003-001.mp4"},
            ),
            "C3531325142202-003-002.mp4",
        )
        self.assertTrue(
            fifty_hour_store.clip_serial_taken_by_other_camera(
                "C3531325142202-003-001.mp4",
                {"C3461325829225-003-001.mp4"},
            )
        )

    def test_next_clip_serial_continues_after_other_camera(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation["camera_serial"] = "C3531325142202"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        subtask_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        for serial in range(1, 21):
            (
                dest / f"C3461325829225-{subtask_id}-{serial:03d}.mp4"
            ).write_bytes(b"other-camera")

        name = fifty_hour_store.next_clip_filename(
            self.video, "applying-sticker", dest
        )
        self.assertEqual(name, f"C3531325142202-{subtask_id}-021.mp4")
        name2 = fifty_hour_store.next_clip_filename(
            self.video,
            "applying-sticker",
            dest,
            reserved={name},
        )
        self.assertEqual(name2, f"C3531325142202-{subtask_id}-022.mp4")

    def test_next_clip_uses_next_free_serial_not_stale_manifest(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation["camera_serial"] = "C3531325142202"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        subtask_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        for serial in range(1, 31):
            (
                dest / f"C3531325142202-{subtask_id}-{serial:03d}.mp4"
            ).write_bytes(b"existing")
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": f"C3461325829225-{subtask_id}-085.mp4",
                    "source_video": "GX072170.MP4",
                    "subtask": "applying-sticker",
                    "camera_serial": "C3461325829225",
                    "video_serial": 85,
                }
            ],
        )
        (dest / f"C3531325142202-{subtask_id}-086.mp4").write_bytes(b"gap")

        name = fifty_hour_store.next_clip_filename(
            self.video, "applying-sticker", dest
        )
        self.assertEqual(name, f"C3531325142202-{subtask_id}-031.mp4")

    def test_relabel_unlabeled_clip_moves_file_and_updates_subtask_id(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="Unlabeled task",
            root=self.root,
        )
        annotation["camera_serial"] = "C3461325829225"
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            self.video, "Unlabeled task"
        )
        unlabeled_dir.mkdir(parents=True, exist_ok=True)
        old_name = "C3461325829225-001-053.mp4"
        (unlabeled_dir / old_name).write_bytes(b"clip")
        annotation["segments"][0]["clip_filename"] = old_name
        annotation["segments"][0]["subtask_id"] = "001"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": old_name,
                    "source_video": self.video.name,
                    "subtask": "Unlabeled task",
                    "camera_serial": "C3461325829225",
                }
            ],
        )
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "sticker placing"
        )

        updated = fifty_hour_store.update_segment(
            self.video,
            annotation["segments"][0]["id"],
            label="sticker placing",
            root=self.root,
        )

        new_id = fifty_hour_store.subtask_id_for_label(self.video, "sticker placing")
        expected = f"C3461325829225-{new_id}-053.mp4"
        new_dir = fifty_hour_store.subtask_export_directory(
            self.video, "sticker placing"
        )
        self.assertEqual(updated["segments"][0]["label"], "sticker placing")
        self.assertEqual(updated["segments"][0]["clip_filename"], expected)
        self.assertEqual(updated["segments"][0]["subtask_id"], new_id)
        self.assertTrue((new_dir / expected).is_file())
        self.assertFalse((unlabeled_dir / old_name).exists())
        manifest = fifty_hour_store.load_manifest(self.task)
        sticker = next(row for row in manifest["subtasks"] if row["name"] == "sticker placing")
        self.assertEqual(sticker["clips"][0]["filename"], expected)

    def test_relabel_moves_clip_without_stored_filename(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="Unlabeled task",
            root=self.root,
        )
        annotation["camera_serial"] = "C3461325829225"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            self.video, "Unlabeled task"
        )
        unlabeled_dir.mkdir(parents=True, exist_ok=True)
        old_name = "C3461325829225-001-053.mp4"
        (unlabeled_dir / old_name).write_bytes(b"clip")
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": old_name,
                    "source_video": self.video.name,
                    "subtask": "Unlabeled task",
                    "camera_serial": "C3461325829225",
                }
            ],
        )
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "sticker placing"
        )

        updated = fifty_hour_store.update_segment(
            self.video,
            annotation["segments"][0]["id"],
            label="sticker placing",
            root=self.root,
        )

        new_id = fifty_hour_store.subtask_id_for_label(self.video, "sticker placing")
        expected = f"C3461325829225-{new_id}-053.mp4"
        new_dir = fifty_hour_store.subtask_export_directory(
            self.video, "sticker placing"
        )
        self.assertEqual(updated["segments"][0]["clip_filename"], expected)
        self.assertTrue((new_dir / expected).is_file())
        self.assertFalse((unlabeled_dir / old_name).exists())
        sidecar = json.loads((self.task / "GX010001.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["segments"][0]["clip_filename"], expected)
        self.assertEqual(sidecar["segments"][0]["subtask_id"], new_id)

    def test_relabel_removes_duplicate_copy_left_in_unlabeled(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="Unlabeled task",
            root=self.root,
        )
        annotation["camera_serial"] = "C3461325829225"
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            self.video, "Unlabeled task"
        )
        unlabeled_dir.mkdir(parents=True, exist_ok=True)
        old_name = "C3461325829225-001-053.mp4"
        (unlabeled_dir / old_name).write_bytes(b"from-unlabeled")
        annotation["segments"][0]["clip_filename"] = old_name
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": old_name,
                    "source_video": self.video.name,
                    "subtask": "Unlabeled task",
                    "camera_serial": "C3461325829225",
                }
            ],
        )
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "sticker placing"
        )
        dest_dir = fifty_hour_store.subtask_export_directory(
            self.video, "sticker placing"
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / old_name).write_bytes(b"duplicate-copy")

        fifty_hour_store.update_segment(
            self.video,
            annotation["segments"][0]["id"],
            label="sticker placing",
            root=self.root,
        )

        new_id = fifty_hour_store.subtask_id_for_label(self.video, "sticker placing")
        expected = f"C3461325829225-{new_id}-053.mp4"
        self.assertTrue((dest_dir / expected).is_file())
        self.assertFalse((unlabeled_dir / old_name).exists())
        self.assertFalse((dest_dir / old_name).exists())

    def test_load_writes_missing_clip_filename_into_json(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="Unlabeled task",
            root=self.root,
        )
        annotation["camera_serial"] = "C3461325829225"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            self.video, "Unlabeled task"
        )
        unlabeled_dir.mkdir(parents=True, exist_ok=True)
        old_name = "C3461325829225-001-007.mp4"
        (unlabeled_dir / old_name).write_bytes(b"clip")
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": old_name,
                    "source_video": self.video.name,
                    "subtask": "Unlabeled task",
                    "camera_serial": "C3461325829225",
                    "source_start": "1.000",
                    "source_end": "2.000",
                }
            ],
        )

        loaded = fifty_hour_store.load_annotation(self.video, root=self.root)
        self.assertEqual(loaded["segments"][0]["clip_filename"], old_name)
        sidecar = json.loads((self.task / "GX010001.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["segments"][0]["clip_filename"], old_name)

    def test_opening_labeled_video_moves_unlabeled_clip(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="Unlabeled task",
            root=self.root,
        )
        annotation["camera_serial"] = "C3461325829225"
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "sticker placing"
        )
        annotation["segments"][0]["label"] = "sticker placing"
        for key in ("clip_filename", "clip_path", "subtask_id", "clip_serial"):
            annotation["segments"][0].pop(key, None)
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            self.video, "Unlabeled task"
        )
        unlabeled_dir.mkdir(parents=True, exist_ok=True)
        old_name = "C3461325829225-001-053.mp4"
        (unlabeled_dir / old_name).write_bytes(b"clip")
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": old_name,
                    "source_video": self.video.name,
                    "subtask": "Unlabeled task",
                    "camera_serial": "C3461325829225",
                }
            ],
        )

        loaded = fifty_hour_store.load_annotation(self.video, root=self.root)
        new_id = fifty_hour_store.subtask_id_for_label(self.video, "sticker placing")
        expected = f"C3461325829225-{new_id}-053.mp4"
        dest_dir = fifty_hour_store.subtask_export_directory(
            self.video, "sticker placing"
        )
        self.assertEqual(loaded["segments"][0]["clip_filename"], expected)
        self.assertTrue((dest_dir / expected).is_file())
        self.assertFalse((unlabeled_dir / old_name).exists())

    def test_labeled_folder_copy_renames_and_leaves_unlabeled(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="Unlabeled task",
            root=self.root,
        )
        annotation["camera_serial"] = "C3461325829225"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            self.video, "Unlabeled task"
        )
        dest_dir = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        unlabeled_dir.mkdir(parents=True, exist_ok=True)
        dest_dir.mkdir(parents=True, exist_ok=True)
        old_name = "C3461325829225-001-053.mp4"
        (unlabeled_dir / old_name).write_bytes(b"from-unlabeled")
        (dest_dir / old_name).write_bytes(b"from-applying")
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": old_name,
                    "source_video": self.video.name,
                    "subtask": "Unlabeled task",
                    "camera_serial": "C3461325829225",
                },
                {
                    "clip_filename": old_name,
                    "source_video": self.video.name,
                    "subtask": "applying-sticker",
                    "camera_serial": "C3461325829225",
                },
            ],
        )

        loaded = fifty_hour_store.load_annotation(self.video, root=self.root)
        new_id = fifty_hour_store.subtask_id_for_label(self.video, "applying-sticker")
        expected = f"C3461325829225-{new_id}-053.mp4"
        self.assertEqual(loaded["segments"][0]["label"], "applying-sticker")
        self.assertEqual(loaded["segments"][0]["clip_filename"], expected)
        self.assertTrue((dest_dir / expected).is_file())
        self.assertFalse((unlabeled_dir / old_name).exists())
        self.assertFalse((dest_dir / old_name).exists())
        self.assertFalse(list(unlabeled_dir.glob("C346*.mp4")))

    def test_other_camera_unlabeled_clip_stays_put(self) -> None:
        other = self.task / "GX020399.MP4"
        other.write_bytes(b"other")
        fifty_hour_store.add_segment(
            other,
            start=1.0,
            end=2.0,
            label="Unlabeled task",
            root=self.root,
        )
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="Unlabeled task",
            root=self.root,
        )
        annotation["camera_serial"] = "C3461325829225"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            other, "Unlabeled task"
        )
        dest_dir = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        unlabeled_dir.mkdir(parents=True, exist_ok=True)
        dest_dir.mkdir(parents=True, exist_ok=True)
        stay = "C3531325142202-001-001.mp4"
        move = "C3461325829225-001-053.mp4"
        (unlabeled_dir / stay).write_bytes(b"keep-unlabeled")
        (unlabeled_dir / move).write_bytes(b"move-me")
        (dest_dir / move).write_bytes(b"already-labeled")
        fifty_hour_store.write_export_manifest(
            other,
            [
                {
                    "clip_filename": stay,
                    "source_video": other.name,
                    "subtask": "Unlabeled task",
                    "camera_serial": "C3531325142202",
                }
            ],
        )
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": move,
                    "source_video": self.video.name,
                    "subtask": "applying-sticker",
                    "camera_serial": "C3461325829225",
                }
            ],
        )

        other_loaded = fifty_hour_store.load_annotation(other, root=self.root)
        self.assertEqual(other_loaded["segments"][0]["label"], "Unlabeled task")
        self.assertTrue((unlabeled_dir / stay).is_file())
        fifty_hour_store.load_annotation(self.video, root=self.root)
        self.assertFalse((unlabeled_dir / move).exists())

    def test_same_serial_in_different_subtasks_is_not_deleted(self) -> None:
        fifty_hour_store.add_label(self.root, "garment-folding-general", "opening-sticker")
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        opening_id = fifty_hour_store.subtask_id_for_label(
            self.video, "opening-sticker"
        )
        applying_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        opening = fifty_hour_store.subtask_export_directory(
            self.video, "opening-sticker"
        )
        applying = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        opening.mkdir(parents=True, exist_ok=True)
        applying.mkdir(parents=True, exist_ok=True)
        left = f"C3531325142202-{opening_id}-001.mp4"
        right = f"C3531325142202-{applying_id}-001.mp4"
        (opening / left).write_bytes(b"opening-clip")
        (applying / right).write_bytes(b"applying-clip")
        fifty_hour_store.write_export_manifest(
            self.video,
            [
                {
                    "clip_filename": left,
                    "source_video": self.video.name,
                    "subtask": "opening-sticker",
                    "camera_serial": "C3531325142202",
                },
                {
                    "clip_filename": right,
                    "source_video": self.video.name,
                    "subtask": "applying-sticker",
                    "camera_serial": "C3531325142202",
                },
            ],
        )

        fifty_hour_store.load_manifest(self.task)
        self.assertTrue((opening / left).is_file())
        self.assertTrue((applying / right).is_file())
        self.assertEqual((opening / left).read_bytes(), b"opening-clip")
        self.assertEqual((applying / right).read_bytes(), b"applying-clip")

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

    def test_add_segment_extends_when_too_close_to_previous_mark(self) -> None:
        start, end = fifty_hour_store._resolve_non_overlapping_start(
            2.0, 2.03, [{"id": 1, "start": 1.0, "end": 2.0}]
        )
        self.assertAlmostEqual(start, 2.01, places=3)
        self.assertGreaterEqual(end - start, 0.05 - 1e-6)

    def test_update_segment_matches_numeric_string_id(self) -> None:
        self.assertEqual(fifty_hour_store._segment_id_key("51.0"), "51")
        self.assertEqual(fifty_hour_store._segment_id_key(51), "51")
        found = fifty_hour_store._find_segment(
            {"segments": [{"id": 51, "label": "Unlabeled task"}]},
            "51.0",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["label"], "Unlabeled task")


if __name__ == "__main__":
    unittest.main()

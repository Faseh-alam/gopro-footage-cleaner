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

    def test_opening_video_does_not_create_json(self) -> None:
        fifty_hour_store.load_annotation(self.video, root=self.root)
        json_files = [path.name for path in self.task.glob("*.json")]
        tmp_files = [path.name for path in self.task.glob("*.tmp")]
        self.assertEqual(json_files, [])
        self.assertEqual(tmp_files, [])

    def test_save_keeps_one_video_json_and_one_manifest(self) -> None:
        from gopro_cleaner.core import annotation_store, scaleai_store

        fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="pick-cloth",
            root=self.root,
        )
        annotation_store.save_annotation(self.video, {"segments": []})
        scaleai_store.save_annotation(
            self.video, scaleai_store.empty_annotation(self.video)
        )
        names = sorted(
            path.name
            for path in self.task.iterdir()
            if path.suffix.lower() in {".json", ".tmp", ".txt"}
        )
        self.assertEqual(names, ["GX010001.json", "manifest.json"])
        self.assertFalse(list(self.task.rglob("*.tmp")))
        self.assertFalse((self.task / "GX010001.segments.json").exists())
        self.assertFalse((self.task / "GX010001.scaleai.json").exists())

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

    def test_add_segment_writes_progress_without_manual_refresh(self) -> None:
        fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.5,
            label="pick-cloth",
            segment_type="subtask",
            root=self.root,
        )
        progress = fifty_hour_store.load_progress(self.root, refresh=False)
        row = progress["tasks"][0]
        self.assertAlmostEqual(row["labeled_hours"], 1.5 / 3600.0, places=4)

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
        stitched = self.task / f"{folder}-stitched.mp4"
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
        (self.task / ".GX010001.json.tmp").write_text("{}", encoding="utf-8")
        (self.task / "GX010001.json.tmp").write_text("{}", encoding="utf-8")
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
        self.assertFalse((self.task / ".GX010001.json.tmp").exists())
        self.assertFalse((self.task / "GX010001.json.tmp").exists())
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

    def test_compact_folder_serials_continue_across_cameras(self) -> None:
        other = self.task / "GX010002.MP4"
        other.write_bytes(b"fake-mp4")
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        first = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        first["camera_serial"] = "C3531325142202"
        first["segments"][0]["clip_filename"] = (
            f"C3531325142202-{dest_id}-001.mp4"
        )
        fifty_hour_store.save_annotation(self.video, first, root=self.root)
        second = fifty_hour_store.add_segment(
            other,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        second["camera_serial"] = "C3461325829225"
        second["segments"][0]["clip_filename"] = (
            f"C3461325829225-{dest_id}-001.mp4"
        )
        fifty_hour_store.save_annotation(other, second, root=self.root)
        for serial in range(1, 4):
            (
                dest / f"C3531325142202-{dest_id}-{serial:03d}.mp4"
            ).write_bytes(b"video-one")
        for serial in range(1, 3):
            (
                dest / f"C3461325829225-{dest_id}-{serial:03d}.mp4"
            ).write_bytes(b"video-two")

        manifest = fifty_hour_store.load_manifest(self.task, repair=False)
        fifty_hour_store._compact_labeled_clip_serials(self.task, manifest)
        names = {path.name for path in dest.glob("*.mp4")}
        self.assertEqual(
            names,
            {
                f"C3531325142202-{dest_id}-001.mp4",
                f"C3531325142202-{dest_id}-002.mp4",
                f"C3531325142202-{dest_id}-003.mp4",
                f"C3461325829225-{dest_id}-004.mp4",
                f"C3461325829225-{dest_id}-005.mp4",
            },
        )
        rewritten = json.loads(
            (self.task / "GX010002.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            rewritten["segments"][0]["clip_filename"],
            f"C3461325829225-{dest_id}-004.mp4",
        )
        self.assertEqual(rewritten["segments"][0]["clip_serial"], 4)

    def test_next_clip_starts_after_earlier_video_label_count(self) -> None:
        other = self.task / "GX010002.MP4"
        other.write_bytes(b"fake-mp4")
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        first = None
        for index in range(3):
            first = fifty_hour_store.add_segment(
                self.video,
                start=float(index + 1),
                end=float(index + 2),
                label="applying-sticker",
                root=self.root,
            )
        first["camera_serial"] = "C3531325142202"
        fifty_hour_store.save_annotation(self.video, first, root=self.root)
        second = fifty_hour_store.add_segment(
            other,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        second["camera_serial"] = "C3461325829225"
        fifty_hour_store.save_annotation(other, second, root=self.root)

        name = fifty_hour_store.next_clip_filename(
            other, "applying-sticker", dest
        )
        self.assertEqual(name, f"C3461325829225-{dest_id}-004.mp4")
        held = fifty_hour_store.serials_reserved_by_earlier_videos(
            other, "applying-sticker"
        )
        self.assertEqual(held, {1, 2, 3})

    def test_compact_reserves_label_count_for_earlier_camera(self) -> None:
        other = self.task / "GX010002.MP4"
        other.write_bytes(b"fake-mp4")
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        first = None
        for index in range(5):
            first = fifty_hour_store.add_segment(
                self.video,
                start=float(index + 1),
                end=float(index + 2),
                label="applying-sticker",
                root=self.root,
            )
        first["camera_serial"] = "C3531325142202"
        fifty_hour_store.save_annotation(self.video, first, root=self.root)
        second = fifty_hour_store.add_segment(
            other,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        second["camera_serial"] = "C3461325829225"
        fifty_hour_store.save_annotation(other, second, root=self.root)
        (dest / f"C3531325142202-{dest_id}-001.mp4").write_bytes(b"one-a")
        (dest / f"C3531325142202-{dest_id}-002.mp4").write_bytes(b"one-b")
        (dest / f"C3461325829225-{dest_id}-001.mp4").write_bytes(b"two-a")
        (dest / f"C3461325829225-{dest_id}-002.mp4").write_bytes(b"two-b")

        manifest = fifty_hour_store.load_manifest(self.task, repair=False)
        fifty_hour_store._compact_labeled_clip_serials(self.task, manifest)
        names = {path.name for path in dest.glob("*.mp4")}
        self.assertEqual(
            names,
            {
                f"C3531325142202-{dest_id}-001.mp4",
                f"C3531325142202-{dest_id}-002.mp4",
                f"C3461325829225-{dest_id}-006.mp4",
                f"C3461325829225-{dest_id}-007.mp4",
            },
        )
        self.assertEqual(
            (dest / f"C3531325142202-{dest_id}-001.mp4").read_bytes(), b"one-a"
        )
        self.assertEqual(
            (dest / f"C3461325829225-{dest_id}-006.mp4").read_bytes(), b"two-a"
        )

    def test_next_clip_ignores_ghost_json_names_without_files(self) -> None:
        other = self.task / "GX010002.MP4"
        other.write_bytes(b"fake-mp4")
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        first = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        first["camera_serial"] = "C3531325142202"
        first["segments"][0]["clip_filename"] = (
            f"C3531325142202-{dest_id}-077.mp4"
        )
        fifty_hour_store.save_annotation(self.video, first, root=self.root)
        second = fifty_hour_store.add_segment(
            other,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        second["camera_serial"] = "C3461325829225"
        second["segments"][0]["clip_filename"] = (
            f"C3461325829225-{dest_id}-055.mp4"
        )
        fifty_hour_store.save_annotation(other, second, root=self.root)
        (dest / f"C3531325142202-{dest_id}-001.mp4").write_bytes(b"keep")

        name = fifty_hour_store.next_clip_filename(
            self.video, "applying-sticker", dest
        )
        self.assertEqual(name, f"C3531325142202-{dest_id}-002.mp4")

    def test_clip_download_audit_missing_and_extra(self) -> None:
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        annotation = None
        for index in range(2):
            annotation = fifty_hour_store.add_segment(
                self.video,
                start=float(index + 1),
                end=float(index + 2),
                label="applying-sticker",
                root=self.root,
            )
        annotation["camera_serial"] = "C3531325142202"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        (dest / f"C3531325142202-{dest_id}-001.mp4").write_bytes(b"one")
        (dest / f"C3461325829225-{dest_id}-001.mp4").write_bytes(b"other-cam")

        audit = fifty_hour_store.clip_download_audit(self.video)
        row = next(
            item
            for item in audit["subtasks"]
            if item["label"] == "applying-sticker"
        )
        self.assertEqual(row["labeled"], 2)
        self.assertEqual(row["downloaded"], 1)
        self.assertEqual(row["missing"], 1)
        self.assertEqual(row["extra"], 0)
        self.assertFalse(audit["ok"])

        (dest / f"C3531325142202-{dest_id}-002.mp4").write_bytes(b"two")
        audit = fifty_hour_store.clip_download_audit(self.video)
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["labeled"], 2)
        self.assertEqual(audit["downloaded"], 2)

        (dest / f"C3531325142202-{dest_id}-003.mp4").write_bytes(b"extra")
        audit = fifty_hour_store.clip_download_audit(self.video)
        row = next(
            item
            for item in audit["subtasks"]
            if item["label"] == "applying-sticker"
        )
        self.assertEqual(row["extra"], 1)
        self.assertFalse(audit["ok"])

    def test_clip_download_audit_flags_unneeded_unlabeled_clips(self) -> None:
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", fifty_hour_store.UNLABELED_TASK_LABEL
        )
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
        dest_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        (dest / f"C3531325142202-{dest_id}-001.mp4").write_bytes(b"keep")
        unlabeled = fifty_hour_store.subtask_export_directory(
            self.video, fifty_hour_store.UNLABELED_TASK_LABEL
        )
        unlabeled.mkdir(parents=True, exist_ok=True)
        unlabeled_id = fifty_hour_store.subtask_id_for_label(
            self.video, fifty_hour_store.UNLABELED_TASK_LABEL
        )
        (unlabeled / f"C3531325142202-{unlabeled_id}-031.mp4").write_bytes(b"extra")

        audit = fifty_hour_store.clip_download_audit(self.video)
        row = next(
            item
            for item in audit["subtasks"]
            if item["label"].lower() == fifty_hour_store.UNLABELED_TASK_LABEL.lower()
        )
        self.assertEqual(row["labeled"], 0)
        self.assertGreaterEqual(row["downloaded"], 1)
        self.assertGreaterEqual(row["extra"], 1)
        self.assertFalse(audit["ok"])

    def test_place_named_clip_does_not_delete_other_camera(self) -> None:
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(
            self.video, "applying-sticker"
        )
        first_name = f"C3531325142202-{dest_id}-001.mp4"
        second_name = f"C3461325829225-{dest_id}-001.mp4"
        (dest / first_name).write_bytes(b"video-one")
        (dest / second_name).write_bytes(b"video-two")
        fifty_hour_store.place_named_clip(
            self.video, second_name, dest, first_name
        )
        self.assertTrue((dest / first_name).is_file())
        self.assertEqual((dest / first_name).read_bytes(), b"video-one")
        self.assertTrue((dest / second_name).is_file())
        self.assertEqual((dest / second_name).read_bytes(), b"video-two")

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
        expected = f"C3461325829225-{new_id}-001.mp4"
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
        self.assertTrue((new_dir / expected).is_file())

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
        expected = f"C3461325829225-{new_id}-001.mp4"
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
        expected = f"C3461325829225-{new_id}-001.mp4"
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
        expected = f"C3461325829225-{new_id}-001.mp4"
        dest_dir = fifty_hour_store.subtask_export_directory(
            self.video, "sticker placing"
        )
        self.assertEqual(loaded["segments"][0]["clip_filename"], expected)
        self.assertTrue((dest_dir / expected).is_file())
        self.assertFalse((unlabeled_dir / old_name).exists())

    def test_labeled_folder_copy_of_unlabeled_clip_is_evicted(self) -> None:
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
        self.assertEqual(loaded["segments"][0]["label"], "Unlabeled task")
        self.assertTrue((unlabeled_dir / old_name).is_file())
        self.assertFalse((dest_dir / old_name).exists())
        self.assertFalse(list(dest_dir.glob("C346*.mp4")))

    def test_unlabeled_clip_left_in_labeled_folder_moves_back(self) -> None:
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="Unlabeled task",
            root=self.root,
        )
        annotation["camera_serial"] = "C3531325142202"
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            self.video, "Unlabeled task"
        )
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest_dir = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(self.video, "applying-sticker")
        wrong = f"C3531325142202-{dest_id}-035.mp4"
        (dest_dir / wrong).write_bytes(b"should-be-unlabeled")
        annotation["segments"][0]["clip_filename"] = "C3531325142202-001-035.mp4"
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)

        loaded = fifty_hour_store.load_annotation(self.video, root=self.root)
        self.assertEqual(loaded["segments"][0]["label"], "Unlabeled task")
        self.assertFalse((dest_dir / wrong).exists())
        unlabeled_clips = list(unlabeled_dir.glob("C353*.mp4"))
        self.assertEqual(len(unlabeled_clips), 1)
        self.assertEqual(unlabeled_clips[0].read_bytes(), b"should-be-unlabeled")

    def test_inflight_unclaimed_clips_stay_while_labels_need_files(self) -> None:
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(self.video, "applying-sticker")
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=3.0,
            end=4.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation["camera_serial"] = "C3531325142202"
        keep = f"C3531325142202-{dest_id}-001.mp4"
        inflight = f"C3531325142202-{dest_id}-020.mp4"
        annotation["segments"][0]["clip_filename"] = keep
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        (dest / keep).write_bytes(b"first-applying")
        (dest / inflight).write_bytes(b"inflight-applying")

        fifty_hour_store.load_annotation(self.video, root=self.root)

        unlabeled = list(self.task.glob("Unlabeled-task-*/C353*.mp4"))
        self.assertEqual(unlabeled, [])
        remaining = {path.read_bytes() for path in dest.glob("C353*.mp4")}
        self.assertEqual(remaining, {b"first-applying", b"inflight-applying"})
        for path in dest.glob("C353*.mp4"):
            parsed = fifty_hour_store.CLIP_NAME_RE.match(path.name)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.group("subtask"), dest_id)

    def test_json_named_clip_reclaimed_from_unlabeled(self) -> None:
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "Unlabeled task"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(self.video, "applying-sticker")
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=3.0,
            end=4.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation["camera_serial"] = "C3531325142202"
        keep = f"C3531325142202-{dest_id}-001.mp4"
        named = f"C3531325142202-{dest_id}-020.mp4"
        annotation["segments"][0]["clip_filename"] = keep
        annotation["segments"][1]["clip_filename"] = named
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        (dest / keep).write_bytes(b"first-applying")
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            self.video, "Unlabeled task"
        )
        unlabeled_dir.mkdir(parents=True, exist_ok=True)
        unlabeled_id = fifty_hour_store.subtask_id_for_label(
            self.video, "Unlabeled task"
        )
        misplaced = f"C3531325142202-{unlabeled_id}-020.mp4"
        (unlabeled_dir / misplaced).write_bytes(b"should-be-applying")

        fifty_hour_store.load_annotation(self.video, root=self.root)

        self.assertFalse((unlabeled_dir / misplaced).exists())
        self.assertEqual(
            {path.read_bytes() for path in dest.glob("C353*.mp4")},
            {b"first-applying", b"should-be-applying"},
        )
        for path in dest.glob("C353*.mp4"):
            parsed = fifty_hour_store.CLIP_NAME_RE.match(path.name)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.group("subtask"), dest_id)
        leftover = list(unlabeled_dir.glob("C353*.mp4")) if unlabeled_dir.is_dir() else []
        self.assertEqual(leftover, [])

    def test_ghost_json_names_do_not_dump_gap_fill_clips(self) -> None:
        """JSON still naming 077–078 must not send inflight 002–003 to Unlabeled-task."""
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "Unlabeled task"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(self.video, "applying-sticker")
        unlabeled_id = fifty_hour_store.subtask_id_for_label(
            self.video, "Unlabeled task"
        )
        annotation = None
        for start in (1.0, 3.0, 5.0):
            annotation = fifty_hour_store.add_segment(
                self.video,
                start=start,
                end=start + 1.0,
                label="applying-sticker",
                root=self.root,
            )
        annotation["camera_serial"] = "C3531325142202"
        annotation["segments"][0]["clip_filename"] = (
            f"C3531325142202-{dest_id}-001.mp4"
        )
        annotation["segments"][1]["clip_filename"] = (
            f"C3531325142202-{dest_id}-077.mp4"
        )
        annotation["segments"][2]["clip_filename"] = (
            f"C3531325142202-{dest_id}-078.mp4"
        )
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        (dest / f"C3531325142202-{dest_id}-001.mp4").write_bytes(b"keep-001")
        (dest / f"C3531325142202-{dest_id}-002.mp4").write_bytes(b"gap-002")
        (dest / f"C3531325142202-{dest_id}-003.mp4").write_bytes(b"gap-003")

        fifty_hour_store.load_annotation(self.video, root=self.root)

        unlabeled = list(self.task.glob("Unlabeled-task-*/C353*.mp4"))
        self.assertEqual(unlabeled, [])
        remaining = {path.read_bytes() for path in dest.glob("C353*.mp4")}
        self.assertEqual(remaining, {b"keep-001", b"gap-002", b"gap-003"})
        for path in dest.glob("C353*.mp4"):
            parsed = fifty_hour_store.CLIP_NAME_RE.match(path.name)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.group("subtask"), dest_id)
            self.assertNotEqual(parsed.group("subtask"), unlabeled_id)

    def test_wrong_id_in_labeled_folder_is_kept_while_short(self) -> None:
        """C353-008-020 in applying-sticker must not move to Unlabeled-task at 1/2."""
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "Unlabeled task"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(self.video, "applying-sticker")
        unlabeled_id = fifty_hour_store.subtask_id_for_label(
            self.video, "Unlabeled task"
        )
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=3.0,
            end=4.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation["camera_serial"] = "C3531325142202"
        keep = f"C3531325142202-{dest_id}-001.mp4"
        annotation["segments"][0]["clip_filename"] = keep
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        (dest / keep).write_bytes(b"first-applying")
        misplaced = f"C3531325142202-{unlabeled_id}-020.mp4"
        (dest / misplaced).write_bytes(b"inflight-applying")

        fifty_hour_store.load_annotation(self.video, root=self.root)

        unlabeled = list(self.task.glob("Unlabeled-task-*/C353*.mp4"))
        self.assertEqual(unlabeled, [])
        remaining = {path.read_bytes() for path in dest.glob("C353*.mp4")}
        self.assertEqual(remaining, {b"first-applying", b"inflight-applying"})
        for path in dest.glob("C353*.mp4"):
            parsed = fifty_hour_store.CLIP_NAME_RE.match(path.name)
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed.group("subtask"), dest_id)

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
        self.assertTrue((unlabeled_dir / stay).is_file())
        self.assertTrue((unlabeled_dir / move).is_file())
        self.assertFalse((dest_dir / move).exists())

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
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="opening-sticker",
            root=self.root,
        )
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=3.0,
            end=4.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation["camera_serial"] = "C3531325142202"
        for segment in annotation["segments"]:
            if str(segment.get("label") or "") == "opening-sticker":
                segment["clip_filename"] = left
            elif str(segment.get("label") or "") == "applying-sticker":
                segment["clip_filename"] = right
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
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

    def test_manifest_clip_rows_match_files_on_disk(self) -> None:
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(self.video, "applying-sticker")
        keep = f"C3461325829225-{dest_id}-036.mp4"
        ghost = f"C3461325829225-{dest_id}-099.mp4"
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation["camera_serial"] = "C3461325829225"
        annotation["segments"][0]["clip_filename"] = keep
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        (dest / keep).write_bytes(b"on-disk")
        manifest = fifty_hour_store.load_manifest(self.task, repair=False)
        applying = next(
            row
            for row in manifest["subtasks"]
            if str(row.get("name") or "").lower() == "applying-sticker"
        )
        applying["clips"] = [
            {
                "camera_serial": "C3461325829225",
                "video_serial": 36,
                "filename": keep,
                "source_video": self.video.name,
            },
            {
                "camera_serial": "C3461325829225",
                "video_serial": 99,
                "filename": ghost,
                "source_video": self.video.name,
            },
        ]
        applying["total_clips"] = 2
        fifty_hour_store._save_manifest(self.task, manifest)

        loaded = fifty_hour_store.load_manifest(self.task)
        applying = next(
            row
            for row in loaded["subtasks"]
            if str(row.get("name") or "").lower() == "applying-sticker"
        )
        names = [str(clip.get("filename") or "") for clip in applying.get("clips") or []]
        compacted = f"C3461325829225-{dest_id}-001.mp4"
        self.assertEqual(names, [compacted])
        self.assertEqual(applying["total_clips"], 1)
        self.assertTrue((dest / compacted).is_file())
        self.assertFalse((dest / ghost).exists())

    def test_unclaimed_dest_id_leftovers_leave_labeled_folder(self) -> None:
        fifty_hour_store.add_label(
            self.root, "garment-folding-general", "applying-sticker"
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        dest_id = fifty_hour_store.subtask_id_for_label(self.video, "applying-sticker")
        keep = f"C3461325829225-{dest_id}-036.mp4"
        dupe = f"C3461325829225-{dest_id}-037.mp4"
        extra = f"C3461325829225-{dest_id}-099.mp4"
        annotation = fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        annotation["camera_serial"] = "C3461325829225"
        annotation["segments"][0]["clip_filename"] = keep
        fifty_hour_store.save_annotation(self.video, annotation, root=self.root)
        (dest / keep).write_bytes(b"real-applying-clip")
        (dest / dupe).write_bytes(b"real-applying-clip")
        (dest / extra).write_bytes(b"unique-leftover")

        fifty_hour_store.load_annotation(self.video, root=self.root)

        compacted = f"C3461325829225-{dest_id}-001.mp4"
        self.assertTrue((dest / compacted).is_file())
        self.assertFalse((dest / keep).exists())
        self.assertFalse((dest / dupe).exists())
        self.assertFalse((dest / extra).exists())
        unlabeled_dir = fifty_hour_store.subtask_export_directory(
            self.video, "Unlabeled task"
        )
        unlabeled = sorted(
            path.read_bytes() for path in unlabeled_dir.glob("C346*.mp4")
        )
        self.assertEqual(unlabeled, [b"real-applying-clip", b"unique-leftover"])
        self.assertEqual((dest / compacted).read_bytes(), b"real-applying-clip")

    def test_nested_stitched_file_moves_next_to_manifest(self) -> None:
        fifty_hour_store.add_segment(
            self.video,
            start=1.0,
            end=2.0,
            label="applying-sticker",
            root=self.root,
        )
        dest = fifty_hour_store.subtask_export_directory(
            self.video, "applying-sticker"
        )
        dest.mkdir(parents=True, exist_ok=True)
        folder = dest.name
        nested = dest / f"{folder}-stitched.mp4"
        nested.write_bytes(b"stitched-body")

        fifty_hour_store.load_manifest(self.task)

        target = self.task / f"{folder}-stitched.mp4"
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), b"stitched-body")
        self.assertFalse(nested.exists())
        self.assertFalse(fifty_hour_store._is_source_video_file(target))

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

    def test_resolve_too_close_when_duration_has_no_room(self) -> None:
        with self.assertRaisesRegex(ValueError, "Too close to a previous mark"):
            fifty_hour_store._resolve_non_overlapping_start(
                2.0,
                2.03,
                [{"id": 1, "start": 1.0, "end": 2.0}],
                duration=2.04,
            )

    def test_relabel_does_not_reject_touching_neighbor(self) -> None:
        fifty_hour_store.add_segment(
            self.video, start=1.0, end=2.0, label="Unlabeled task", root=self.root
        )
        annotation = fifty_hour_store.add_segment(
            self.video, start=2.0, end=3.5, label="Unlabeled task", root=self.root
        )
        second_id = annotation["segments"][1]["id"]
        updated = fifty_hour_store.update_segment(
            self.video, second_id, label="opening-sticker", root=self.root
        )
        second = next(row for row in updated["segments"] if row["id"] == second_id)
        self.assertEqual(second["label"], "opening-sticker")
        self.assertAlmostEqual(second["start"], 2.01, places=3)
        self.assertAlmostEqual(second["end"], 3.5, places=3)

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

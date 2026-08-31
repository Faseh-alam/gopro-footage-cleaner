"""API tests for task-level ScaleAI segment and manifest files."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gopro_cleaner.app import create_app


class FiftyHourRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "50HoursVideos"
        self.task = self.root / "PackagingBoxes"
        self.task.mkdir(parents=True)
        self.first = self.task / "video1.mp4"
        self.second = self.task / "video2.mp4"
        self.first.write_bytes(b"first")
        self.second.write_bytes(b"second")
        self.duration_patch = patch(
            "gopro_cleaner.core.fifty_hour_store.resolve_media_duration",
            return_value=30.0,
        )
        self.media_patch = patch(
            "gopro_cleaner.core.fifty_hour_store._media_fields",
            return_value={
                "camera_serial": "CAM001",
                "cl_number": None,
                "media_meta": {"camera_serial": "CAM001"},
            },
        )
        self.duration_patch.start()
        self.media_patch.start()
        app = create_app()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.media_patch.stop()
        self.duration_patch.stop()
        self.tmp.cleanup()

    def _add_segment(self, video: Path, label: str, start: float) -> None:
        response = self.client.post(
            "/api/eager/scaleai/segments",
            json={
                "path": str(video),
                "root": str(self.root),
                "start": start,
                "end": start + 1.0,
                "label": label,
                "type": "subtask",
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_first_video_defines_manifest_for_later_videos_and_clip_sequence(self) -> None:
        initial = self.client.get(
            "/api/eager/scaleai/annotation",
            query_string={"path": str(self.first), "root": str(self.root)},
        )
        self.assertEqual(initial.get_json()["labels"], [])

        self._add_segment(self.first, "Picking up the box", 1.0)
        second = self.client.get(
            "/api/eager/scaleai/annotation",
            query_string={"path": str(self.second), "root": str(self.root)},
        )
        self.assertEqual(second.get_json()["labels"], ["Picking up the box"])
        self._add_segment(self.second, "Picking up the box", 3.0)

        queued = SimpleNamespace(source_has_gpmf=True)
        with patch(
            "gopro_cleaner.eager_routes.eager_trim_queue.submit",
            return_value=queued,
        ):
            for video in (self.first, self.second):
                response = self.client.post(
                    "/api/eager/scaleai/process-video",
                    json={"path": str(video), "root": str(self.root)},
                )
                self.assertEqual(response.status_code, 200, response.get_json())

        # A missing output is rebuilt with its recorded serial, not a new number.
        with patch(
            "gopro_cleaner.eager_routes.eager_trim_queue.submit",
            return_value=queued,
        ) as rebuilt:
            response = self.client.post(
                "/api/eager/scaleai/process-video",
                json={"path": str(self.first), "root": str(self.root)},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            rebuilt.call_args.kwargs["output_filename"],
            "CAM001-001-001.mp4",
        )

        first = json.loads((self.task / "video1.json").read_text(encoding="utf-8"))
        second = json.loads((self.task / "video2.json").read_text(encoding="utf-8"))
        self.assertEqual(first["segments"][0]["clip_filename"], "CAM001-001-001.mp4")
        self.assertEqual(second["segments"][0]["clip_filename"], "CAM001-001-002.mp4")
        self.assertFalse((self.task / "segment.json").exists())

        manifest = json.loads((self.task / "manifest.json").read_text(encoding="utf-8"))
        subtask = manifest["subtasks"][0]
        self.assertEqual(subtask["id"], "001")
        self.assertEqual(subtask["name"], "Picking up the box")
        self.assertEqual(subtask["folder"], "Picking-up-the-box-001")
        self.assertEqual(subtask["total_clips"], 2)
        self.assertEqual(
            [clip["filename"] for clip in subtask["clips"]],
            ["CAM001-001-001.mp4", "CAM001-001-002.mp4"],
        )

        subtask_dir = self.task / subtask["folder"]
        for clip in subtask["clips"]:
            (subtask_dir / clip["filename"]).write_bytes(b"trimmed")
        stitch_result = SimpleNamespace(
            ok=True,
            task="Picking up the box",
            output=str(subtask_dir / "Picking-up-the-box-001-stitched.mp4"),
            clip_count=2,
            duration=2.0,
            has_gpmf=True,
            message="stitched",
            error=None,
        )
        with patch(
            "gopro_cleaner.eager_routes.stitch_task_clips",
            return_value=stitch_result,
        ) as stitch:
            response = self.client.post(
                "/api/eager/scaleai/stitch-video",
                json={"path": str(self.second), "overwrite": True},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["stitched"], 1)
        call = stitch.call_args
        self.assertEqual([path.name for path in call.kwargs["clips"]], [
            "CAM001-001-001.mp4",
            "CAM001-001-002.mp4",
        ])
        self.assertEqual(
            call.kwargs["output"].name,
            "Picking-up-the-box-001-stitched.mp4",
        )
        self.assertEqual(call.kwargs["output"].parent, self.task)

    def test_process_video_counts_not_downloaded_until_complete(self) -> None:
        self._add_segment(self.first, "Picking up the box", 1.0)
        queued = SimpleNamespace(source_has_gpmf=True, job_id="trim-watch-1")
        with patch(
            "gopro_cleaner.eager_routes.eager_trim_queue.submit",
            return_value=queued,
        ):
            response = self.client.post(
                "/api/eager/scaleai/process-video",
                json={"path": str(self.first), "root": str(self.root)},
            )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertEqual(payload["job_ids"], ["trim-watch-1"])
        batch = payload["export_batch"]
        self.assertEqual(batch["downloaded"], 0)
        self.assertEqual(batch["not_downloaded"], 1)
        self.assertFalse(batch["all_success"])
        active = self.client.get("/api/eager/trim/active").get_json()
        self.assertEqual(active["export_batch"]["not_downloaded"], 1)
        self.assertFalse(active["export_batch"]["all_success"])
        self.assertEqual(active["export_batch"]["source_path"], str(self.first.resolve()))

    def test_duplicate_clip_filename_queues_a_second_file(self) -> None:
        self._add_segment(self.first, "Picking up the box", 1.0)
        self._add_segment(self.first, "Picking up the box", 3.0)
        sidecar = self.task / "video1.json"
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["segments"][0]["clip_filename"] = "CAM001-001-001.mp4"
        payload["segments"][1]["clip_filename"] = "CAM001-001-001.mp4"
        sidecar.write_text(json.dumps(payload), encoding="utf-8")
        dest = self.task / "Picking-up-the-box-001"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "CAM001-001-001.mp4").write_bytes(b"first-clip")
        queued = SimpleNamespace(source_has_gpmf=True, job_id="trim-dup-2")
        with patch(
            "gopro_cleaner.eager_routes.eager_trim_queue.submit",
            return_value=queued,
        ) as submit:
            response = self.client.post(
                "/api/eager/scaleai/process-video",
                json={"path": str(self.first), "root": str(self.root)},
            )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertEqual(payload["skipped"], 1)
        self.assertEqual(payload["queued"], 1)
        self.assertEqual(
            submit.call_args.kwargs["output_filename"],
            "CAM001-001-002.mp4",
        )
        saved = json.loads(sidecar.read_text(encoding="utf-8"))
        names = [row["clip_filename"] for row in saved["segments"]]
        self.assertEqual(names, ["CAM001-001-001.mp4", "CAM001-001-002.mp4"])

    def test_process_video_writes_json_before_submit(self) -> None:
        self._add_segment(self.first, "Picking up the box", 1.0)
        sidecar = self.task / "video1.json"
        names_at_submit: list[str] = []

        def _submit(*_args, **_kwargs):
            saved = json.loads(sidecar.read_text(encoding="utf-8"))
            names_at_submit.extend(
                str(row.get("clip_filename") or "") for row in saved["segments"]
            )
            return SimpleNamespace(source_has_gpmf=True, job_id="trim-named")

        with patch(
            "gopro_cleaner.eager_routes.eager_trim_queue.submit",
            side_effect=_submit,
        ):
            response = self.client.post(
                "/api/eager/scaleai/process-video",
                json={"path": str(self.first), "root": str(self.root)},
            )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertEqual(payload["queued"], 1)
        self.assertEqual(names_at_submit, ["CAM001-001-001.mp4"])

    def test_add_segment_returns_refreshed_progress(self) -> None:
        response = self.client.post(
            "/api/eager/scaleai/segments",
            json={
                "path": str(self.first),
                "root": str(self.root),
                "start": 1.0,
                "end": 2.5,
                "label": "Picking up the box",
                "type": "subtask",
            },
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        tasks = (payload.get("progress") or {}).get("tasks") or []
        self.assertTrue(tasks)
        row = next(item for item in tasks if item.get("task") == "PackagingBoxes")
        self.assertAlmostEqual(row["labeled_hours"], 1.5 / 3600.0, places=4)


if __name__ == "__main__":
    unittest.main()

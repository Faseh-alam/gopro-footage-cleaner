"""API smoke tests for task-level ScaleAI JSON labeling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_cleaner.app import create_app


class ScaleAIRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "50 hours"
        self.task_dir = self.root / "AWS" / "Button Sewing"
        self.task_dir.mkdir(parents=True)
        self.video = self.task_dir / "GX010001.MP4"
        self.video.write_bytes(b"fake")
        self.duration_patch = patch(
            "gopro_cleaner.core.fifty_hour_store.resolve_media_duration",
            return_value=30.0,
        )
        self.duration_patch.start()
        app = create_app()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.duration_patch.stop()
        self.tmp.cleanup()

    def test_task_level_subtask_flow(self) -> None:
        response = self.client.get(
            "/api/eager/scaleai/annotation",
            query_string={"path": str(self.video), "root": str(self.root)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["annotation"]["parent_task"], "Button Sewing")

        response = self.client.post(
            "/api/eager/scaleai/segments",
            json={
                "path": str(self.video),
                "root": str(self.root),
                "label": "pick-up-button",
                "start": 2.1,
                "end": 2.6,
            },
        )
        self.assertEqual(response.status_code, 200)
        annotation = response.get_json()["annotation"]
        self.assertEqual(annotation["segments"][0]["label"], "pick-up-button")
        self.assertTrue((self.task_dir / "GX010001.json").is_file())
        self.assertTrue((self.task_dir / "manifest.json").is_file())
        self.assertFalse((self.task_dir / "segment.json").exists())
        self.assertFalse(self.video.with_name("GX010001.segments.json").exists())

    def test_video_delete_removes_video_json_keeps_manifest(self) -> None:
        response = self.client.post(
            "/api/eager/scaleai/segments",
            json={
                "path": str(self.video),
                "root": str(self.root),
                "label": "pick-up-button",
                "start": 2.1,
                "end": 2.6,
            },
        )
        self.assertEqual(response.status_code, 200)
        video_sidecar = self.task_dir / "GX010001.json"
        with patch("gopro_cleaner.eager_routes.move_to_trash") as move:
            response = self.client.post(
                "/api/eager/video/delete",
                json={"path": str(self.video), "confirmed": True},
            )
        self.assertEqual(response.status_code, 200)
        moved = [call.args[0] for call in move.call_args_list]
        self.assertIn(self.video.resolve(), moved)
        self.assertFalse(video_sidecar.exists())
        self.assertTrue((self.task_dir / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()

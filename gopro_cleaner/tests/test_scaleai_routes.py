"""API smoke tests for two-stage ScaleAI JSON-only labeling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_cleaner.app import create_app


class ScaleAIRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.tmp.name) / "50 hours" / "AWS" / "Button Sewing"
        self.task_dir.mkdir(parents=True)
        self.video = self.task_dir / "GX010001.MP4"
        self.video.write_bytes(b"fake")
        self.duration_patch = patch(
            "gopro_cleaner.core.scaleai_store.resolve_media_duration",
            return_value=30.0,
        )
        self.duration_patch.start()
        app = create_app()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.duration_patch.stop()
        self.tmp.cleanup()

    def test_parent_then_contained_subtask_flow(self) -> None:
        response = self.client.get(
            "/api/eager/scaleai/annotation", query_string={"path": str(self.video)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["annotation"]["parent_task"], "Button Sewing")

        response = self.client.post(
            "/api/eager/scaleai/parent-cycles",
            json={"path": str(self.video), "start": 2.0, "end": 8.0},
        )
        self.assertEqual(response.status_code, 200)
        cycle_id = response.get_json()["annotation"]["parent_cycles"][0]["id"]

        response = self.client.post(
            "/api/eager/scaleai/subtask-segments",
            json={
                "path": str(self.video),
                "cycle_id": cycle_id,
                "task": "pick-up-button",
                "start": 2.1,
                "end": 2.6,
            },
        )
        self.assertEqual(response.status_code, 200)
        annotation = response.get_json()["annotation"]
        self.assertEqual(annotation["subtask_names"], ["pick-up-button"])
        self.assertEqual(annotation["subtask_segments"][0]["parent_cycle_id"], cycle_id)

        response = self.client.post(
            "/api/eager/scaleai/subtask-segments",
            json={
                "path": str(self.video),
                "cycle_id": cycle_id,
                "task": "outside",
                "start": 1.0,
                "end": 2.2,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("inside parent cycle", response.get_json()["error"])

        self.assertTrue(self.video.with_name("GX010001.scaleai.json").is_file())
        self.assertFalse(self.video.with_name("GX010001.segments.json").exists())

    def test_video_delete_moves_scaleai_sidecar_with_source(self) -> None:
        scaleai_sidecar = self.video.with_name("GX010001.scaleai.json")
        scaleai_sidecar.write_text("{}", encoding="utf-8")
        with patch("gopro_cleaner.eager_routes.move_to_trash") as move:
            response = self.client.post(
                "/api/eager/video/delete",
                json={"path": str(self.video), "confirmed": True},
            )
        self.assertEqual(response.status_code, 200)
        moved = [call.args[0] for call in move.call_args_list]
        self.assertIn(self.video.resolve(), moved)
        self.assertIn(scaleai_sidecar.resolve(), moved)


if __name__ == "__main__":
    unittest.main()

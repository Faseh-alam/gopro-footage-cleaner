"""Counts for the last Trim click: downloaded vs still pending."""

from __future__ import annotations

import threading
import unittest

from gopro_cleaner.core.eager_trim_queue import EagerTrimQueue, EagerTrimRecord


class ExportBatchStatusTests(unittest.TestCase):
    def _queue(self) -> EagerTrimQueue:
        queue = EagerTrimQueue.__new__(EagerTrimQueue)
        queue._lock = threading.Lock()
        queue._records = {}
        queue._export_job_ids = []
        queue._export_already = 0
        queue._export_source_path = ""
        queue._export_folder_wide = False
        return queue

    def test_success_only_when_every_clip_is_completed(self) -> None:
        queue = self._queue()
        queue._records["a"] = EagerTrimRecord(
            job_id="a",
            source_path="video.mp4",
            start_seconds=0,
            end_seconds=1,
            status="queued",
        )
        queue._records["b"] = EagerTrimRecord(
            job_id="b",
            source_path="video.mp4",
            start_seconds=1,
            end_seconds=2,
            status="completed",
        )
        batch = queue.begin_export_batch(
            ["a", "b"], already_downloaded=1, source_path="C:/videos/GX020399.MP4"
        )
        self.assertEqual(batch["downloaded"], 2)
        self.assertEqual(batch["not_downloaded"], 1)
        self.assertEqual(batch["total"], 3)
        self.assertFalse(batch["all_success"])
        self.assertEqual(batch["source_path"], "C:/videos/GX020399.MP4")
        self.assertFalse(batch["folder_wide"])

        queue._records["a"].status = "completed"
        batch = queue.export_batch_status()
        self.assertEqual(batch["downloaded"], 3)
        self.assertEqual(batch["not_downloaded"], 0)
        self.assertTrue(batch["all_success"])

    def test_already_saved_clips_count_as_downloaded(self) -> None:
        queue = self._queue()
        batch = queue.begin_export_batch([], already_downloaded=4)
        self.assertEqual(batch["downloaded"], 4)
        self.assertEqual(batch["not_downloaded"], 0)
        self.assertTrue(batch["all_success"])

    def test_folder_wide_batch_is_marked(self) -> None:
        queue = self._queue()
        batch = queue.begin_export_batch([], already_downloaded=2, folder_wide=True)
        self.assertTrue(batch["folder_wide"])
        self.assertEqual(batch["source_path"], "")


if __name__ == "__main__":
    unittest.main()

"""Preview HLS jobs must not advertise leftover segments while queued."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_cleaner.core import preview_proxy as pp


class PreviewPlayableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "cache"
        self.cache.mkdir()
        self.source = Path(self.tmp.name) / "GX020399.MP4"
        self.source.write_bytes(b"fake-mp4")
        self.cache_patch = patch.object(pp, "_cache_dir", return_value=self.cache)
        self.cache_patch.start()

    def tearDown(self) -> None:
        key = str(self.source.resolve())
        with pp._lock:
            pp._jobs.pop(key, None)
        self.cache_patch.stop()
        self.tmp.cleanup()

    def _write_stale_playlist(self, segments: int = 20) -> None:
        dest = pp._preview_dir(self.source)
        dest.mkdir(parents=True, exist_ok=True)
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:1"]
        for i in range(segments):
            lines.extend(["#EXTINF:1.0,", f"seg{i:05d}.ts"])
        (dest / "index.m3u8").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_queued_job_does_not_mark_stale_segments_playable(self) -> None:
        self._write_stale_playlist()
        key = str(self.source.resolve())
        with pp._lock:
            pp._jobs[key] = {
                "status": "running",
                "progress": 0,
                "process": None,
                "message": "Queued — waiting for the current encode to finish…",
            }

        status = pp.preview_status(self.source, start=False)
        self.assertEqual(status.get("status"), "running")
        self.assertFalse(status.get("playable"))

        with pp._lock:
            pp._jobs[key]["process"] = object()
        status = pp.preview_status(self.source, start=False)
        self.assertTrue(status.get("playable"))
        self.assertEqual(status.get("segments"), 20)

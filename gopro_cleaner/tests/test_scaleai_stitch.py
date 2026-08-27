"""Tests for ScaleAI micro-task stitch planning (no real ffmpeg footage required)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_cleaner.core.probe import MediaInfo, StreamInfo
from gopro_cleaner.core.scaleai_stitch import (
    discover_task_dirs,
    list_task_clips,
    plan_stitch,
)


def _media(path: Path, *, duration: float = 1.0, gpmf: bool = True) -> MediaInfo:
    streams = [
        StreamInfo(0, "video", "hevc", "hvc1", "GoPro H.265"),
        StreamInfo(1, "audio", "aac", "mp4a", "GoPro AAC"),
    ]
    gpmf_index = None
    if gpmf:
        streams.append(StreamInfo(2, "data", "bin_data", "gpmd", "GoPro MET"))
        gpmf_index = 2
    return MediaInfo(
        path=path,
        duration=duration,
        size_bytes=1000,
        streams=streams,
        video_index=0,
        audio_index=1,
        gpmf_index=gpmf_index,
        has_gpmf=gpmf,
    )


class ScaleAIStitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task = self.root / "grab-cloth"
        self.task.mkdir()
        self.a = self.task / "GX010001-1.MP4"
        self.b = self.task / "GX010002-1.MP4"
        self.a.write_bytes(b"a")
        self.b.write_bytes(b"b")
        # Should be ignored by clip listing.
        (self.task / "grab-cloth__stitched.MP4").write_bytes(b"x")
        (self.task / ".hidden.MP4").write_bytes(b"x")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_list_skips_stitched_and_hidden(self) -> None:
        clips = list_task_clips(self.task)
        self.assertEqual([p.name for p in clips], ["GX010001-1.MP4", "GX010002-1.MP4"])

    def test_discover_task_dirs(self) -> None:
        empty = self.root / "empty-task"
        empty.mkdir()
        found = discover_task_dirs(self.root)
        self.assertEqual([p.name for p in found], ["grab-cloth"])

    def test_plan_sums_durations_and_gpmf(self) -> None:
        def fake_probe(path: Path) -> MediaInfo:
            return _media(Path(path), duration=0.4 if "0001" in Path(path).name else 0.6)

        with patch("gopro_cleaner.core.scaleai_stitch.probe_media", side_effect=fake_probe):
            plan = plan_stitch(self.task)
        self.assertEqual(plan.clip_count, 2)
        self.assertAlmostEqual(plan.total_duration, 1.0)
        self.assertTrue(plan.all_have_gpmf)
        self.assertTrue(plan.output.name.endswith("__stitched.MP4"))

    def test_plan_rejects_gpmf_mismatch(self) -> None:
        def fake_probe(path: Path) -> MediaInfo:
            gpmf = "0001" in Path(path).name
            return _media(Path(path), gpmf=gpmf)

        with patch("gopro_cleaner.core.scaleai_stitch.probe_media", side_effect=fake_probe):
            with self.assertRaises(RuntimeError):
                plan_stitch(self.task)


if __name__ == "__main__":
    unittest.main()

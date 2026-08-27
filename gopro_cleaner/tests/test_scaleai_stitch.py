"""Tests for ScaleAI micro-task stitch planning (no real ffmpeg footage required)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gopro_cleaner.core.probe import MediaInfo, StreamInfo
from gopro_cleaner.core.scaleai_stitch import (
    _build_concat_command,
    discover_task_dirs,
    list_task_clips,
    plan_stitch,
    stitch_task_clips,
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

    def test_explicit_clip_list_preserves_order_and_excludes_stale_files(self) -> None:
        with patch(
            "gopro_cleaner.core.scaleai_stitch.probe_media",
            side_effect=lambda path: _media(Path(path)),
        ):
            plan = plan_stitch(self.task, clips=[self.b, self.a])
        self.assertEqual(plan.clips, [self.b.resolve(), self.a.resolve()])
        self.assertNotIn(self.task / "grab-cloth__stitched.MP4", plan.clips)

    def test_plan_rejects_gpmf_mismatch(self) -> None:
        def fake_probe(path: Path) -> MediaInfo:
            gpmf = "0001" in Path(path).name
            return _media(Path(path), gpmf=gpmf)

        with patch("gopro_cleaner.core.scaleai_stitch.probe_media", side_effect=fake_probe):
            with self.assertRaises(RuntimeError):
                plan_stitch(self.task)

    def test_concat_tags_first_data_stream_as_gpmd(self) -> None:
        command = _build_concat_command(
            _media(self.a),
            self.task / "concat.txt",
            self.task / "out.MP4",
        )
        # Video + audio → data stream is tagged as d:2 (matches trimmer).
        tag_index = command.index("-tag:d:2")
        self.assertEqual(command[tag_index + 1], "gpmd")
        # Output muxer forced to mp4 (after codec copy), distinct from concat -f.
        out_f = len(command) - 1 - command[::-1].index("-f")
        self.assertEqual(command[out_f + 1], "mp4")
        self.assertIn("100M", command)

    def test_plan_rejects_trim_missing_expected_gpmf(self) -> None:
        self.a.with_suffix(".scaleai-source.json").write_text(
            json.dumps({"source_has_gpmf": True}),
            encoding="utf-8",
        )

        def fake_probe(path: Path) -> MediaInfo:
            return _media(Path(path), gpmf=False)

        with patch("gopro_cleaner.core.scaleai_stitch.probe_media", side_effect=fake_probe):
            with self.assertRaisesRegex(RuntimeError, "source had GPMF"):
                plan_stitch(self.task)

    def test_manifest_maps_stitched_interval_to_source_cycle(self) -> None:
        self.a.with_suffix(".scaleai-source.json").write_text(
            json.dumps(
                {
                    "source": "/footage/GX010001.MP4",
                    "parent_task": "Label Attachment",
                    "parent_cycle_id": "cycle-1",
                    "start": 5.1,
                    "end": 5.5,
                }
            ),
            encoding="utf-8",
        )

        def fake_probe(path: Path) -> MediaInfo:
            path = Path(path)
            if path.name.endswith("__stitched.MP4"):
                return _media(path, duration=1.0)
            return _media(path, duration=0.4 if "0001" in path.name else 0.6)

        def fake_run(command: list[str], **_kwargs):
            Path(command[-1]).write_bytes(b"stitched")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch("gopro_cleaner.core.scaleai_stitch.probe_media", side_effect=fake_probe),
            patch("gopro_cleaner.core.scaleai_stitch.subprocess.run", side_effect=fake_run),
            patch("gopro_cleaner.core.scaleai_stitch._run_udtacopy"),
        ):
            result = stitch_task_clips(self.task, overwrite=True)

        self.assertTrue(result.ok)
        first = result.manifest["clips"][0]
        self.assertEqual(first["source"], "/footage/GX010001.MP4")
        self.assertEqual(first["parent_cycle_id"], "cycle-1")
        self.assertEqual(first["source_start"], 5.1)
        self.assertEqual(first["stitched_start"], 0.0)
        self.assertEqual(first["stitched_end"], 0.4)


if __name__ == "__main__":
    unittest.main()

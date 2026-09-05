"""Tests for voiceover scan + mux command construction."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_cleaner.core.probe import MediaInfo, StreamInfo
from gopro_cleaner.core import voiceover_store


def _media(path: Path, *, gpmf: bool = True, duration: float = 10.0) -> MediaInfo:
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


class VoiceoverStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "voiceover"
        self.root.mkdir()
        self.cls = self.root / "Repairs"
        self.cls.mkdir()
        self.video = self.cls / "GX010001.MP4"
        self.video.write_bytes(b"fake")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_scan_groups_by_class(self) -> None:
        with patch(
            "gopro_cleaner.core.voiceover_store.probe_media",
            side_effect=lambda p: _media(Path(p)),
        ):
            data = voiceover_store.scan_voiceover_tree(self.root)
        self.assertEqual(data["clip_count"], 1)
        self.assertEqual(data["classes"][0]["name"], "Repairs")
        self.assertEqual(data["classes"][0]["clips"][0]["name"], "GX010001.MP4")

    def test_resolve_prefers_voiceover_child(self) -> None:
        usb = Path(self.tmp.name)
        resolved = voiceover_store.resolve_voiceover_root(usb)
        self.assertEqual(resolved, self.root.resolve())

    def test_mux_command_maps_gpmd(self) -> None:
        audio = Path(self.tmp.name) / "take.webm"
        audio.write_bytes(b"a")
        out = Path(self.tmp.name) / "out.partial.MP4"
        cmd = voiceover_store._build_mux_command(_media(self.video), audio, out)
        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertIn("gpmd", cmd)
        self.assertIn("-f", cmd)
        self.assertEqual(cmd[cmd.index("-f") + 1], "mp4")


if __name__ == "__main__":
    unittest.main()

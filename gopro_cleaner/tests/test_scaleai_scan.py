"""ScaleAI scan must pull from 50 hours/{AWS|Google Drive}, not treat them as tasks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gopro_cleaner.core import eager


class ScaleAIScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "50 hours"
        aws = self.root / "AWS" / "Label Attachment" / "Label Attachment"
        gdrive = self.root / "Google Drive" / "Hem Stitching" / "Hem Stitching"
        aws.mkdir(parents=True)
        gdrive.mkdir(parents=True)
        self.aws_video = aws / "GX010001.MP4"
        self.gdrive_video = gdrive / "GX010002.MP4"
        self.aws_video.write_bytes(b"a")
        self.gdrive_video.write_bytes(b"b")
        # Generated exports must stay hidden from labeling.
        generated = self.root / "AWS" / "_ScaleAI" / "grab-cloth"
        generated.mkdir(parents=True)
        (generated / "GX010001-1.MP4").write_bytes(b"x")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_annotate_no_longer_hides_aws_and_google_drive(self) -> None:
        self.assertFalse(eager.is_under_task_folder(self.aws_video, self.root))
        self.assertFalse(eager.is_under_task_folder(self.gdrive_video, self.root))

    def test_scaleai_scan_finds_both_source_trees(self) -> None:
        with patch("gopro_cleaner.core.eager._probe_duration", return_value=1.0):
            with patch(
                "gopro_cleaner.core.ffmpeg_tools.ffmpeg_available",
                return_value={"ok": True, "hint": ""},
            ):
                videos = eager.scan_mp4_files(self.root, recursive=True, mode="scaleai")
        names = {row["name"] for row in videos}
        self.assertEqual(names, {"GX010001.MP4", "GX010002.MP4"})

    def test_scaleai_scan_skips_generated_exports(self) -> None:
        with patch("gopro_cleaner.core.eager._probe_duration", return_value=1.0):
            with patch(
                "gopro_cleaner.core.ffmpeg_tools.ffmpeg_available",
                return_value={"ok": True, "hint": ""},
            ):
                videos = eager.scan_mp4_files(self.root, recursive=True, mode="scaleai")
        paths = {row["path"] for row in videos}
        self.assertTrue(all("_ScaleAI" not in path for path in paths))


if __name__ == "__main__":
    unittest.main()

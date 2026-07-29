"""Tests for branch-aware one-click updates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from gopro_cleaner.core import self_update


class SelfUpdateTests(unittest.TestCase):
    def test_current_branch(self) -> None:
        with patch.object(self_update, "_git", return_value="testing"):
            self.assertEqual(self_update.current_branch(), "testing")

    def test_pulls_matching_current_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            responses = iter(["testing", "aaaaaaa111", "", "", "bbbbbbb222"])

            with (
                patch.object(self_update, "PROJECT_ROOT", root),
                patch.object(self_update.shutil, "which", return_value="/usr/bin/git"),
                patch.object(self_update, "_dirty_tracked_files", return_value=[]),
                patch.object(self_update, "_git", side_effect=lambda *args: next(responses)) as git,
            ):
                result = self_update.pull_latest_current_branch()

        self.assertEqual(result["branch"], "testing")
        self.assertEqual(result["before"], "aaaaaaa")
        self.assertEqual(result["after"], "bbbbbbb")
        self.assertTrue(result["changed"])
        self.assertEqual(
            git.call_args_list,
            [
                call("symbolic-ref", "--quiet", "--short", "HEAD"),
                call("rev-parse", "HEAD"),
                call("fetch", "origin", "testing"),
                call("reset", "--hard", "origin/testing"),
                call("rev-parse", "HEAD"),
            ],
        )


if __name__ == "__main__":
    unittest.main()

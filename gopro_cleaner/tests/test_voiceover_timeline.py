"""Tests for pause-aware voiceover timeline segments."""

from __future__ import annotations

import unittest

from gopro_cleaner.core.voiceover_timeline import (
    build_segments_from_events,
    segments_total_duration,
)


class TimelineSegmentTests(unittest.TestCase):
    def test_pause_inserts_freeze_of_same_duration(self) -> None:
        events = [
            {"type": "play", "session_t": 0.0, "video_t": 0.0},
            {"type": "pause", "session_t": 20.0, "video_t": 20.0},
            {"type": "resume", "session_t": 35.0, "video_t": 20.0},
            {"type": "stop", "session_t": 50.0, "video_t": 35.0},
        ]
        segs = build_segments_from_events(events, source_duration=120.0)
        freezes = [s for s in segs if s.kind == "freeze"]
        plays = [s for s in segs if s.kind == "play"]
        self.assertEqual(len(freezes), 1)
        self.assertAlmostEqual(freezes[0].duration, 15.0, places=2)
        self.assertAlmostEqual(freezes[0].freeze_at, 20.0, places=2)
        self.assertGreaterEqual(len(plays), 2)
        self.assertAlmostEqual(segments_total_duration(segs), 50.0, places=2)

    def test_past_end_freezes_last_frame(self) -> None:
        events = [
            {"type": "play", "session_t": 0.0, "video_t": 0.0},
            {"type": "stop", "session_t": 15.0, "video_t": 10.0},
        ]
        segs = build_segments_from_events(events, source_duration=10.0)
        total = segments_total_duration(segs)
        self.assertAlmostEqual(total, 15.0, places=2)
        self.assertTrue(any(s.kind == "freeze" for s in segs))


if __name__ == "__main__":
    unittest.main()

"""Nami source emits only changes after a verified first baseline."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".cursor" / "skills" / "polymarket-quote" / "scripts"))
sys.path.insert(0, str(ROOT / ".cursor" / "skills" / "dongqiudi-basketball" / "scripts"))

import dqd_lib  # noqa: E402
import nami_basketball as nami  # noqa: E402


class NamiSourceTest(unittest.TestCase):
    def test_extract_and_change_detection(self) -> None:
        raw = {"match_id": 42, "living": [{"url": "https://tracker.namitiyu.com/?id=12345"}]}
        self.assertEqual(dqd_lib.nami_id_from_match(raw), "12345")
        rows = [{"dongqiudi": {"id": "42", "home": "A", "away": "B", "nami_id": "12345"}}]
        with patch.object(nami, "fetch_variable_detail", side_effect=[
            {"homeScores": {"score": 90}, "awayScores": {"score": 88}},
            {"homeScores": {"score": 92}, "awayScores": {"score": 88}},
            {"homeScores": {"score": 91}, "awayScores": {"score": 88}},
        ]):
            poller = nami.NamiScorePoller(interval_s=1)
            self.assertEqual(poller.poll(rows), [])
            poller._next_poll = 0
            events = poller.poll(rows)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["curr"], {"home": 92, "away": 88})
            self.assertFalse(events[0]["is_reversal"])
            poller._next_poll = 0
            self.assertTrue(poller.poll(rows)[0]["is_reversal"])


if __name__ == "__main__":
    unittest.main()

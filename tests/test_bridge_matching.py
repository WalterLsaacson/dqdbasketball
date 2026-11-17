"""Keep basketball fixture recall improvements inside the existing safety gates."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".cursor" / "skills" / "match-bridge" / "scripts"))

import bridge_lib as bridge  # noqa: E402


def fixture(home: str, away: str, league: str, *, kickoff: str = "2026-11-08 03:00") -> dict:
    return {"home": home, "away": away, "league": league, "kickoff_beijing": kickoff}


class BridgeMatchingTest(unittest.TestCase):
    def test_new_league_aliases_match_without_lowering_thresholds(self) -> None:
        pairs = (
            ("NCAA", "College Basketball", "ncaab"),
            ("美国大学女子篮球联赛", "Women's College Basketball", "ncaaw"),
            ("波篮甲", "PLK", "plk"),
            ("亚海联", "ABA League", "aba"),
            ("法篮甲", "Betclic Élite", "lnb"),
        )
        for dqd_league, pm_league, code in pairs:
            with self.subTest(pm_league=pm_league):
                self.assertEqual(bridge.normalize_league(dqd_league), code)
                self.assertEqual(bridge.normalize_league(pm_league), code)
        self.assertEqual(bridge.DEFAULT_MIN_SIDE, 0.75)
        self.assertEqual(bridge.DEFAULT_MAX_SKEW_MIN, 90)

    def test_mens_and_womens_college_leagues_stay_separate(self) -> None:
        dqd = fixture("Kent State", "Northern Kentucky", "NCAA")
        pm = fixture("Kent State", "Northern Kentucky", "Women's College Basketball")
        self.assertEqual(bridge.score_pair(dqd, pm), 0.0)

    def test_sponsor_aliases_match_only_with_correct_league_and_time(self) -> None:
        dqd = fixture("E.C. PINHEIROS SAO PAOLO", "Sao Jose Unimed", "巴西甲")
        pm = fixture("EC Pinheiros SP", "Sao Jose dos Campos", "NBB")
        self.assertGreaterEqual(bridge.score_pair(dqd, pm), bridge.DEFAULT_MIN_SCORE)
        self.assertEqual(bridge.score_pair(dqd, {**pm, "league": "NBA"}), 0.0)
        self.assertEqual(
            bridge.score_pair(dqd, {**pm, "kickoff_beijing": "2026-11-08 05:00"}),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()

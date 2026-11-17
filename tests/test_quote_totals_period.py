"""A team or half total must never settle from the match's full score."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".cursor" / "skills" / "polymarket-quote" / "scripts"))

import quote_lib  # noqa: E402
from score_reversal import OpenPositionLedger  # noqa: E402


class TotalsPeriodTest(unittest.TestCase):
    def test_one_half_team_total_does_not_use_match_score(self) -> None:
        market = {
            "question": "Pacers 1H Team Total: O/U 53.5",
            "sports_market_type": "totals",
            "outcomes": ["Over", "Under"],
            "clob_token_ids": ["over", "under"],
        }
        self.assertEqual(
            quote_lib._parse_total_market(market["question"], "Pacers", "Nuggets"),
            {"line": 53.5, "period": "1h", "side": "home"},
        )
        base = dict(home="Pacers", away="Nuggets", home_score=43, away_score=64, mode="live")
        self.assertEqual(quote_lib.totals_tokens([market], **base), [])
        self.assertEqual(
            quote_lib.totals_tokens([market], **base, home_half=43, away_half=64),
            [],
        )
        rows = quote_lib.totals_tokens(
            [market], **{**base, "home_score": 65}, home_half=60, away_half=40
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_key"], "home_1h_total_53.5_over")
        self.assertEqual(rows[0]["goals"], 60)

    def test_team_total_without_a_named_team_is_skipped(self) -> None:
        self.assertIsNone(
            quote_lib._parse_total_market(
                "Pacers vs. Nuggets: Team Total O/U 53.5", "Pacers", "Nuggets"
            )
        )

    def test_match_total_remains_match_total(self) -> None:
        self.assertEqual(
            quote_lib._parse_total_market(
                "Pacers vs. Nuggets: O/U 210.5", "Pacers", "Nuggets"
            ),
            {"line": 210.5, "period": "ft", "side": "match"},
        )

    def test_invalid_dry_run_lot_stays_invalid_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "open_positions.json"
            ledger = OpenPositionLedger(path)
            ledger.register_buy(
                match_id="54556361", token_id="bad-half-total",
                market_key="match_total_53.5_over", shares=10, usdc=9.4,
                home_score=43, away_score=64, live=False,
                event_key="score_change|54556361|41-64->43-64",
            )
            ledger.mark_closed("bad-half-total", "54556361", reason="invalid_market_mapping")
            reloaded = OpenPositionLedger(path)
            self.assertTrue(reloaded.invalidated_market("bad-half-total", "54556361"))
            self.assertEqual(reloaded.all_open(), [])


if __name__ == "__main__":
    unittest.main()

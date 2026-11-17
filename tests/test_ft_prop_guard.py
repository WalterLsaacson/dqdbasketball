"""FT props need a newly fetched final DQD boxscore before live purchase."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".cursor" / "skills" / "polymarket-quote" / "scripts"))
sys.path.insert(0, str(ROOT / ".cursor" / "skills" / "dongqiudi-basketball" / "scripts"))

import trade_executor as mod  # noqa: E402
import quote_lib as lib  # noqa: E402
import dqd_lib as dqd  # noqa: E402


class FtPlayerPropGuardTest(unittest.TestCase):
    event_key = "match_finished|54556387|100-107"
    bane_quote = {
        "misprice": True,
        "trade": "buy_win",
        "settlement": "WIN",
        "token_id": "bane-no",
        "family": "player_prop",
        "best_ask": 0.41,
    }

    def executor(self, *, live_ft: bool = False, live_prop: bool = False) -> mod.TradeExecutor:
        obj = mod.TradeExecutor.__new__(mod.TradeExecutor)
        obj.settings = SimpleNamespace(
            enabled=True,
            live_ft=live_ft,
            live_score=False,
            live_prop=live_prop,
            live=live_ft or live_prop,
            allow_extreme_prices=False,
            min_buy_price=0.92,
        )
        obj._lock = threading.RLock()
        obj._done = set()
        obj._record = lambda quote, **fields: fields
        return obj

    def test_live_ft_prop_requires_fresh_final_boxscore(self) -> None:
        result = self.executor(live_ft=True).maybe_trade(
            self.bane_quote, event_key=self.event_key, event_type="match_finished"
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "ft_player_prop_requires_fresh_final_boxscore")
        self.assertTrue(result["live"])

    def test_fresh_final_ft_prop_uses_existing_price_rules(self) -> None:
        result = self.executor(live_ft=True).maybe_trade(
            {**self.bane_quote, "ft_boxscore_final_fresh": True},
            event_key=self.event_key,
            event_type="match_finished",
        )
        self.assertEqual(result["skip_reason"], "buy_price_below_min=0.41<0.92")

    def test_live_inplay_prop_keeps_existing_rules(self) -> None:
        result = self.executor(live_prop=True).maybe_trade(
            self.bane_quote,
            event_key="player_stat_change|54556387|bane|assist:4",
            event_type="player_stat_change",
        )
        self.assertEqual(result["skip_reason"], "buy_price_below_min=0.41<0.92")

    def test_live_ft_prop_cannot_place_rest_bid(self) -> None:
        with patch.object(mod, "rest_enabled", side_effect=AssertionError("rest path reached")):
            result = self.executor(live_ft=True).offer_rest_buy(
                self.bane_quote, event_key=self.event_key
            )
        self.assertIsNone(result)

    def test_other_markets_and_dry_mode_keep_existing_rules(self) -> None:
        dry = self.executor(live_ft=False).maybe_trade(
            self.bane_quote, event_key=self.event_key, event_type="match_finished"
        )
        total = self.executor(live_ft=True).maybe_trade(
            {**self.bane_quote, "family": "totals"},
            event_key=self.event_key,
            event_type="match_finished",
        )
        self.assertEqual(dry["skip_reason"], "buy_price_below_min=0.41<0.92")
        self.assertEqual(total["skip_reason"], "buy_price_below_min=0.41<0.92")


class FreshBoxscoreTest(unittest.TestCase):
    def box(self, status: str, assists: int) -> dict:
        return {"data": {"base": {"status": status}, "players": [
            {"id": 587925, "name": "贝恩", "assist": assists}
        ]}}

    def test_playing_snapshot_cannot_settle_ft_props(self) -> None:
        with patch.object(dqd, "fetch_boxscore", return_value=self.box("Playing", 3)):
            players, status = lib._load_final_box(Path("/unused"), "54556387")
        self.assertEqual(status, "playing")
        self.assertEqual(players, {})

    def test_fresh_played_snapshot_uses_updated_assists(self) -> None:
        def set_name(players):
            players["587925"]["person_en_name"] = "Desmond Bane"

        with patch.object(dqd, "fetch_boxscore", return_value=self.box("Played", 4)), \
             patch.object(dqd, "apply_english_person_names", side_effect=set_name):
            players, status = lib._load_final_box(Path("/unused"), "54556387")
        self.assertEqual(status, "played")
        self.assertEqual(players["587925"]["assist"], 4)

        market = {
            "question": "Desmond Bane: Assists O/U 3.5",
            "sports_market_type": "assists",
            "outcomes": ["Yes", "No"],
            "clob_token_ids": ["bane-yes", "bane-no"],
            "market_id": "6924972",
            "line": 3.5,
        }
        rows = lib.player_prop_tokens(
            [market], player="Desmond Bane", stat="assist", curr=4, mode="ft"
        )
        result = {row["outcome"]: row["settlement"] for row in rows}
        self.assertEqual(result, {"Yes": "WIN", "No": "LOSE"})


if __name__ == "__main__":
    unittest.main()

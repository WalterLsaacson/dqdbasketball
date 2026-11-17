"""One market may use $1 across filled buys and working rest bids."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".cursor" / "skills" / "polymarket-quote" / "scripts"))

import trade_executor as mod  # noqa: E402
from rest_ladder import allocate_rest_ladder  # noqa: E402
from score_reversal import OpenPositionLedger  # noqa: E402


class MarketBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = OpenPositionLedger(Path(self.tmp.name) / "positions.json")

    def buy(self, *, match_id: str = "game-1", token_id: str = "yes", market_key: str = "line-1", usdc: float = 1.0) -> None:
        self.ledger.register_buy(
            match_id=match_id, token_id=token_id, market_key=market_key,
            market_id=f"market:{market_key}", condition_id=f"condition:{market_key}",
            shares=usdc / 0.99, usdc=usdc, home_score=50, away_score=40,
            live=True, event_key=f"score_change|{match_id}|50-40",
        )

    def executor(self) -> mod.TradeExecutor:
        obj = mod.TradeExecutor.__new__(mod.TradeExecutor)
        obj.settings = SimpleNamespace(
            enabled=True, live_score=False, live_prop=False, live_ft=False,
            live=False, allow_extreme_prices=False, min_buy_price=0.92,
            max_usdc=1.0, max_market_usdc=1.0, max_shares=2.0,
            size_tiers=((0.98, 1.0),), size_floor_usdc=1.0,
            max_levels=1, max_slippage=0.03, take_depth="top",
            min_order_shares=0,
        )
        obj.ledger = self.ledger
        obj._lock = threading.RLock()
        obj._done = set()
        obj._buy_blocked_matches = set()
        obj._rest_blocked_matches = set()
        obj.af_mode = "gate"
        obj.af_timeout_s = 90.0
        obj._record = lambda quote, **fields: fields
        return obj

    def quote(self, *, token_id: str, market_key: str, market_id: str = "") -> dict:
        identity = market_id or f"market:{market_key}"
        return {
            "misprice": True, "trade": "buy_win", "settlement": "WIN",
            "token_id": token_id, "market_key": market_key, "match_id": "game-1",
            "market_id": identity,
            "condition_id": f"condition:{identity.removeprefix('market:')}",
            "family": "totals", "best_ask": 0.99, "best_bid": 0.99,
        }

    def test_one_market_blocks_both_outcomes_but_not_another_market(self) -> None:
        self.buy()
        executor = self.executor()
        blocked = executor.maybe_trade(
            self.quote(token_id="no", market_key="line-1-no", market_id="market:line-1"),
            event_key="score_change|game-1|51-40", event_type="score_change",
        )
        self.assertEqual(blocked["skip_reason"], "size_policy_market_budget")

        plan = mod.FillPlan(
            trade="buy_win", side="BUY", take_depth="top", order_type="FAK",
            shares=1.0, usdc=1.0, worst_price=0.99, levels_used=1,
            levels=[{"price": 0.99, "size": 1.0}], skip_reason=None,
        )
        with patch.object(mod, "plan_fill", return_value=plan) as planner:
            allowed = executor.maybe_trade(
                self.quote(token_id="another", market_key="line-2"),
                event_key="score_change|game-1|52-40", event_type="score_change",
            )
        self.assertEqual(allowed["status"], "dry_run")
        self.assertEqual(planner.call_args.kwargs["max_usdc"], 1.0)

    def test_partial_rest_fill_counts_only_unfilled_remainder(self) -> None:
        self.buy(usdc=0.4)
        self.ledger.add_rest_order(
            match_id="game-1", token_id="yes", market_key="line-1",
            market_id="market:line-1", condition_id="condition:line-1",
            order={"order_id": "rest-1", "price": 0.99, "shares": 0.6 / 0.99,
                   "usdc": 0.6, "filled_shares": 0, "filled_usdc": 0,
                   "status": "LIVE"},
        )
        self.assertAlmostEqual(self.ledger.market_buy_commitment_usdc(
            match_id="game-1", market_key="line-1-no", token_id="no",
            market_id="market:line-1"), 1.0)
        self.ledger.apply_rest_fill(
            match_id="game-1", token_id="yes", order_id="rest-1",
            filled_shares=0.2 / 0.99, filled_usdc=0.2, status="LIVE",
        )
        self.assertAlmostEqual(self.ledger.market_buy_commitment_usdc(
            match_id="game-1", market_key="line-1-no", token_id="no",
            market_id="market:line-1"), 1.0)
        self.assertEqual(self.ledger.market_buy_commitment_usdc(
            match_id="game-2", market_key="line-1", token_id="yes"), 0.0)

    def test_rest_bid_cannot_exceed_market_budget(self) -> None:
        self.buy()
        rest_quote = {
            **self.quote(token_id="no", market_key="line-1"),
            "best_ask": None,
        }
        with patch.object(mod, "rest_enabled", return_value=True), \
             patch.object(mod, "allocate_rest_ladder", side_effect=AssertionError("budget bypassed")):
            result = self.executor().offer_rest_buy(
                rest_quote, event_key="score_change|game-1|51-40",
            )
        self.assertIsNone(result)

    def test_one_usdc_rest_bid_respects_five_share_exchange_minimum(self) -> None:
        self.assertEqual(allocate_rest_ladder(
            1.0, tick_size="0.01", best_bid=0.99, best_ask=None,
            min_shares=5.0,
        ), [])


if __name__ == "__main__":
    unittest.main()

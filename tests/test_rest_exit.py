"""Offline regression for matched rest bids and incremental exit sells."""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".cursor" / "skills" / "polymarket-quote" / "scripts"))

from trade_executor import TradeExecutor, flatten_floor_price  # noqa: E402
from trade_settings import load_trade_settings  # noqa: E402
import quote_lib as lib  # noqa: E402


class FakeTrader:
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.sells: list[dict] = []
        self.fill_on_cancel: float | None = None
        self.ready = True

    def get_collateral_usdc(self) -> Decimal:
        return Decimal("50")

    def get_conditional_balance(self, token_id: str) -> Decimal:
        return Decimal("10")

    def post_limit_buy(self, token_id, shares, price, tick_size, **kwargs):
        self.orders["b1"] = {"status": "LIVE", "size_matched": "0"}
        return {"orderID": "b1", "status": "LIVE", "success": True}

    def post_limit_sell(self, token_id, shares, price, tick_size, **kwargs):
        oid = f"s{len(self.sells) + 1}"
        self.sells.append({"id": oid, "shares": float(shares), "price": float(price)})
        self.orders[oid] = {"status": "LIVE", "size_matched": "0"}
        return {"orderID": oid, "status": "LIVE", "success": True}

    def get_order(self, order_id):
        return dict(self.orders[order_id])

    def cancel_order(self, order_id):
        if self.fill_on_cancel is not None:
            self.orders[order_id]["size_matched"] = str(self.fill_on_cancel)
        self.orders[order_id]["status"] = "CANCELED"
        return {"canceled": [order_id]}

    @staticmethod
    def is_order_success(response, *, market=True):
        return bool(response.get("success"))


class RestExitTest(unittest.TestCase):
    def setUp(self) -> None:
        async_patch = patch.object(lib, "append_jsonl_async", side_effect=lib.append_jsonl)
        async_patch.start()
        self.addCleanup(async_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.trader = FakeTrader()
        settings = replace(load_trade_settings(), live_score=True, live_ft=True)
        self.executor = TradeExecutor(self.root, settings, trader=self.trader)
        self.quote = {
            "settlement": "WIN", "token_id": "token-1", "family": "totals",
            "market_key": "Over 4.5", "best_ask": 0.996,
            "best_bid": 0.98, "tick_size": "0.001", "book_missing": False,
        }
        self.meta = {
            "match_id": "42", "home": "Home", "away": "Away",
            "home_score": 5, "away_score": 0, "event_type": "score_change",
        }
        self.key = "score_change|42|0-0->5-0"

    def test_partial_fill_is_booked_once_and_exit_is_not_duplicated(self) -> None:
        first = self.executor.offer_rest_buy(self.quote, event_key=self.key, match_meta=self.meta)
        self.assertEqual(first["status"], "posted")
        self.assertIsNone(self.executor.offer_rest_buy(self.quote, event_key=self.key, match_meta=self.meta))
        self.trader.orders["b1"] = {"status": "MATCHED", "size_matched": "5.05"}
        self.assertEqual(self.executor.reconcile_rest_orders(), 1)
        self.assertEqual(self.executor.reconcile_rest_orders(), 0)
        lot = self.executor.ledger.open_for_match("42")[0]
        self.assertAlmostEqual(lot["shares"], 5.05)
        self.assertAlmostEqual(lot["exit_eligible_shares"], 5.05)
        self.assertEqual(self.executor.offer_exit_sells(), 1)
        self.assertEqual(self.trader.sells[0]["price"], 0.999)
        self.assertEqual(self.executor.offer_exit_sells(), 0)
        self.assertEqual(len(self.trader.sells), 1)
        self.trader.orders["s1"] = {"status": "MATCHED", "size_matched": "5.05"}
        self.assertEqual(self.executor.offer_exit_sells(), 0)
        self.assertEqual(self.executor.ledger.open_for_match("42"), [])

    def test_cancel_books_partial_fill_before_stopping_rest(self) -> None:
        self.executor.offer_rest_buy(self.quote, event_key=self.key, match_meta=self.meta)
        self.trader.orders["b1"] = {"status": "LIVE", "size_matched": "2"}
        self.assertEqual(self.executor.cancel_rest_orders("42"), 1)
        lot = self.executor.ledger.open_for_match("42")[0]
        self.assertAlmostEqual(lot["shares"], 2.0)
        self.assertEqual(self.executor.ledger.live_rest_orders(match_id="42"), [])

    def test_cancel_books_fill_that_arrives_during_cancel(self) -> None:
        self.executor.offer_rest_buy(self.quote, event_key=self.key, match_meta=self.meta)
        self.trader.fill_on_cancel = 2.0
        self.assertEqual(self.executor.cancel_rest_orders("42"), 1)
        lot = self.executor.ledger.open_for_match("42")[0]
        self.assertAlmostEqual(lot["shares"], 2.0)
        self.assertAlmostEqual(lot["usdc"], 1.98)
        self.assertEqual(self.executor.ledger.live_rest_orders(match_id="42"), [])
        self.assertEqual(self.executor.reconcile_rest_orders(), 0)

    def test_099_and_0995_asks_use_fak_while_higher_ask_uses_rest(self) -> None:
        dry_settings = replace(
            self.executor.settings, live_score=False, live_prop=False, live_ft=False,
            allow_extreme_prices=False,
        )
        dry_executor = TradeExecutor(self.root, dry_settings, trader=self.trader)
        tokens = [
            {"settlement": "WIN", "token_id": f"token-{i}", "family": "totals",
             "market_key": f"Over {i}.5"}
            for i in range(3)
        ]
        books = {
            f"token-{i}": {
                "book_missing": False, "best_ask": ask, "best_ask_size": 10,
                "best_bid": 0.98, "tick_size": "0.001",
            }
            for i, ask in enumerate((0.99, 0.995, 0.996))
        }
        priced = lib.quote_tokens(
            tokens, books=books, trade_executor=dry_executor,
            event_key_str=self.key, match_meta=self.meta, trade_workers=1,
        )
        for item in priced[:2]:
            self.assertTrue(item["misprice"])
            self.assertEqual(item["trade_attempt"]["status"], "dry_run")
            self.assertNotIn("rest_attempt", item)
        self.assertFalse(priced[2]["misprice"])
        self.assertEqual(priced[2]["rest_attempt"]["status"], "dry_run")

    def test_walk_depth_stops_at_win_fak_ceiling(self) -> None:
        dry_settings = replace(
            self.executor.settings, live_score=False, live_prop=False, live_ft=False,
            take_depth="walk", allow_extreme_prices=False,
        )
        dry_executor = TradeExecutor(self.root, dry_settings, trader=self.trader)
        priced = lib.quote_tokens(
            [{"settlement": "WIN", "token_id": "walk-token", "family": "totals",
              "market_key": "Over 4.5"}],
            books={"walk-token": {
                "book_missing": False, "best_ask": 0.99, "best_ask_size": 1,
                "best_bid": 0.98, "tick_size": "0.001",
                "asks_top": [
                    {"price": "0.99", "size": "1"},
                    {"price": "0.999", "size": "10"},
                ],
            }},
            trade_executor=dry_executor, event_key_str=self.key,
            match_meta=self.meta, trade_workers=1,
        )
        plan = priced[0]["trade_attempt"]["plan"]
        self.assertEqual(priced[0]["trade_attempt"]["status"], "dry_run")
        self.assertEqual(plan["worst_price"], 0.99)
        self.assertEqual(plan["levels_used"], 1)

    def test_unsettled_token_never_gets_rest_bid(self) -> None:
        self.quote["settlement"] = "OPEN"
        self.assertIsNone(self.executor.offer_rest_buy(self.quote, event_key=self.key, match_meta=self.meta))
        self.assertFalse(self.trader.orders)

    def test_flatten_floor_tracks_entry_cost(self) -> None:
        lot = {"shares": 5.0, "usdc": 4.95}
        self.assertEqual(flatten_floor_price(lot, "0.001"), Decimal("0.792"))
        self.assertEqual(flatten_floor_price(lot, "0.01"), Decimal("0.80"))


if __name__ == "__main__":
    unittest.main()

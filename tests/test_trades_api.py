"""Ledger API filters and raw detail use the same persisted trade records."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "frontend" / "main" / "server" / "app.py"
spec = importlib.util.spec_from_file_location("basketball_main_app", APP)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class TradesApiTest(unittest.TestCase):
    def test_filters_and_raw_detail(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            data = root / "data" / "pm-quote"
            data.mkdir(parents=True)
            rows = [
                {"quoted_at": "2026-11-07T10:00:00+08:00", "status": "posted",
                 "trade": "rest_buy", "live": True, "match_id": "42",
                 "home": "Lakers", "away": "Warriors", "market_key": "Over 200.5",
                 "idempotency_key": "a", "plan": {"usdc": 5.0, "shares": 5.05}},
                {"quoted_at": "2026-11-07T11:00:00+08:00", "status": "dry_run",
                 "trade": "buy_win", "live": False, "match_id": "43",
                 "home": "Spurs", "away": "Kings", "market_key": "Over 210.5",
                 "idempotency_key": "b", "plan": {"usdc": 2.0, "shares": 2.1}},
            ]
            (data / "trades.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
            )
            (data / "open_positions.json").write_text(json.dumps({
                "positions": [{"status": "open", "shares": 5.05, "usdc": 5.0}]
            }), encoding="utf-8")
            old = app.ROOT
            app.ROOT = root
            try:
                payload = app.trades_ledger_payload({"live": ["1"], "q": ["Lakers"], "limit": ["5"]})
                self.assertEqual(payload["returned"], 1)
                self.assertEqual(payload["trades"][0]["idempotency_key"], "a")
                self.assertEqual(payload["opens"]["open_usdc"], 5.0)
                self.assertIn("0.99", payload["trades"][0]["note"])
                detail = app.trade_detail_payload({"idempotency_key": ["a"]})
                self.assertEqual(detail["raw"], rows[0])
                self.assertFalse(app.trade_detail_payload({"idempotency_key": ["none"]})["ok"])
                server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{server.server_port}"
                    with urllib.request.urlopen(base + "/api/trades?live=1&limit=5") as response:
                        self.assertEqual(json.load(response)["returned"], 1)
                    with urllib.request.urlopen(base + "/trades") as response:
                        self.assertIn(b"Trades Ledger", response.read())
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)
            finally:
                app.ROOT = old


if __name__ == "__main__":
    unittest.main()

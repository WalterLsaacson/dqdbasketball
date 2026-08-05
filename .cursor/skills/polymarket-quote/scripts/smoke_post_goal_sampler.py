#!/usr/bin/env python3
"""Smoke: post-goal sampler enqueue, parallel schedule, settle/reversal helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from post_goal_sampler import (  # noqa: E402
    SAMPLE_COUNT,
    SAMPLE_INTERVAL_S,
    PostGoalSampler,
    _TokenSpec,
    get_active_sampler,
    settle_token_at_score,
)


def main() -> int:
    assert SAMPLE_COUNT == 6
    assert SAMPLE_INTERVAL_S == 10.0

    # Settlement recompute: Over 2.5 locked at 3-0, unlocked after reverse to 2-0
    tok = _TokenSpec(
        token_id="t",
        market_key="match_total_2.5_over",
        family="totals",
        outcome="Over",
        settlement_t0="WIN",
        question="O/U 2.5",
        trade_status="dry_run",
        trade_live=False,
        plan_usdc=1.0,
        plan_shares=1.0,
        t0_best_ask=0.9,
        t0_best_bid=0.88,
        t0_net_edge=0.05,
        t0_gross_edge=0.1,
        t0_fee=0.05,
        t0_misprice=True,
        line=2.5,
        total_side="match",
        total_period="ft",
        is_over=True,
    )
    s, locked = settle_token_at_score(tok, home_score=3, away_score=0)
    assert s == "WIN" and locked
    s2, locked2 = settle_token_at_score(tok, home_score=2, away_score=0)
    assert s2 is None and not locked2

    # Reversal helper: only undoes t0
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data" / "pm-quote").mkdir(parents=True)
        bdir = root / "data" / "bridge"
        bdir.mkdir(parents=True)
        # Unrelated later reversal on same match must NOT count for t0=1-0
        (bdir / "events.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "score_change",
                            "match_id": "m1",
                            "ts": "2026-07-26T22:00:10+08:00",
                            "is_reversal": True,
                            "prev": {"home": 2, "away": 0},
                            "curr": {"home": 1, "away": 0},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "score_change",
                            "match_id": "m1",
                            "ts": "2026-07-26T22:00:20+08:00",
                            "is_reversal": True,
                            "prev": {"home": 1, "away": 0},
                            "curr": {"home": 0, "away": 0},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        sampler = PostGoalSampler(root)
        rev = sampler._reversal_state(
            match_id="m1",
            after_iso="2026-07-26T22:00:00+08:00",
            score_home=1,
            score_away=0,
        )
        assert rev["seen"] is True
        assert rev["path"] == "1-0→0-0"
        rev_miss = sampler._reversal_state(
            match_id="m1",
            after_iso="2026-07-26T22:00:00+08:00",
            score_home=3,
            score_away=0,
        )
        assert rev_miss["seen"] is False

        sampler.start()
        assert get_active_sampler() is sampler

        bundle = {
            "quoted_at": "2026-07-26T22:00:00+08:00",
            "trigger": "score_change",
            "match_id": "m1",
            "event_key": "score_change|m1|0-0->1-0|t",
            "home": "Home",
            "away": "Away",
            "home_score": 1,
            "away_score": 0,
            "quotes": [
                {
                    "token_id": "tokA",
                    "market_key": "match_total_0.5_over",
                    "family": "totals",
                    "outcome": "Over",
                    "settlement": "WIN",
                    "question": "O/U 0.5",
                    "line": 0.5,
                    "total_side": "match",
                    "total_period": "ft",
                    "trade": "buy_win",
                    "misprice": True,
                    "best_ask": 0.9,
                    "best_bid": 0.88,
                    "net_edge": 0.05,
                    "gross_edge": 0.1,
                    "fee": 0.05,
                    "trade_attempt": {
                        "status": "dry_run",
                        "success": True,
                        "live": False,
                        "plan": {"usdc": 1.0, "shares": 1.1},
                    },
                },
            ],
        }
        n = sampler.enqueue_from_bundle(
            bundle, eps=0.005, fee_rate=0.0, min_net=0.02, proxy=None
        )
        assert n == 1, n

        path = root / "data" / "pm-quote" / "post_goal_samples.jsonl"
        deadline = time.time() + 2
        while time.time() < deadline and not path.is_file():
            time.sleep(0.05)
        rows = [
            json.loads(l)
            for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        assert rows[0]["sample_i"] == 0
        assert rows[0]["settlement_t0"] == "WIN"
        assert "settlement_changed" in rows[0]
        assert rows[0]["reversal_seen"] is True  # 1-0→0-0 after t0

        sampler.stop()
        assert get_active_sampler() is None

    print("ok: post_goal_sampler parallel/settle/reversal fixes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

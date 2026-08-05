#!/usr/bin/env python3
"""Smoke checks for basketball overtime score forwarding and FT transitions."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import bridge_lib as bl  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _row(period: str, home_score: int, away_score: int) -> dict:
    return {
        "dongqiudi": {
            "id": "game-1",
            "home": "Los Angeles Lakers",
            "away": "Golden State Warriors",
            "status_raw": "Playing" if period != "FT" else "Played",
            "status": "Playing" if period != "FT" else "Played",
            "period": period,
            "home_score": home_score,
            "away_score": away_score,
            "official_clock": period,
        },
        "polymarket": {
            "home": "Los Angeles Lakers",
            "away": "Golden State Warriors",
            "league": "NBA",
            "event_id": "1",
            "slug": "nba-lal-gsw",
            "market_refs": [],
            "props": [],
        },
        "kickoff_beijing": "2026-08-06 10:00",
    }


def main() -> int:
    _assert(bl.is_full_time({"period": "FT"}), "FT identifies full time")
    _assert(not bl.is_full_time({"period": "OT"}), "OT is not full time")

    # Overtime changes are normal basketball score events and must propagate.
    prev_scores = {"game-1": {"home": 100, "away": 100}}
    overtime_events = bl.detect_score_changes([_row("OT", 103, 100)], prev_scores)
    _assert(len(overtime_events) == 1, "OT score delta must emit")
    _assert(overtime_events[0]["type"] == "score_change", "score event type")
    _assert(overtime_events[0]["curr"] == {"home": 103, "away": 100}, "OT score")

    prev_status = {"game-1": "playing"}
    prev_period = {"game-1": "OT"}
    finished = bl.detect_match_finished([_row("FT", 112, 108)], prev_status, prev_period)
    _assert(len(finished) == 1, "OT → FT must emit match_finished")
    _assert(finished[0]["period"] == "FT", "finished period")
    print("smoke_ft_period: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

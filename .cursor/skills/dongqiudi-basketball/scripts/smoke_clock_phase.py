#!/usr/bin/env python3
"""Smoke tests for basketball score, FT, and player-stat event semantics."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dqd_lib as lib  # noqa: E402


def match(**overrides):
    row = {
        "id": "1", "cmp_type": "basketball", "home": "A", "away": "B",
        "home_score": 40, "away_score": 38, "league": "Test", "league_id": "1",
        "status_raw": "Playing", "period": "Q2", "minute_str": "03:12",
        "home_fs_4": "", "away_fs_4": "", "score_diff": 2, "score_total": 78,
    }
    row.update(overrides)
    return row


def main() -> int:
    baseline = {"1": {"home": 40, "away": 38, "period": "Q2"}}
    events = lib.detect_score_changes([match(home_score=42, score_diff=4, score_total=80)], baseline, "hot")
    assert len(events) == 1
    assert events[0]["type"] == "score_change"
    assert events[0]["is_score_up"] is True and events[0]["is_goal"] is True
    assert events[0]["period"] == "Q2"

    finished = lib.detect_score_changes(
        [match(home_score=42, period="FT", status_raw="Played", score_diff=4, score_total=80)],
        baseline,
        "hot",
    )
    assert len(finished) == 1 and finished[0]["type"] == "match_finished"

    current = {"9": {"person_id": "9", "person_name": "P", "person_en_name": "Player",
                     "score": 10, "rebound": 4, "assist": 2, "steal": 1, "block": 0}}
    prior = {"9": {"score": 8, "rebound": 4, "assist": 1, "steal": 1, "block": 0}}
    stat_events = lib.detect_player_stat_changes(match(), current, prior, "full")
    assert stat_events[0]["changes"] == {"score": 2, "assist": 1}
    print("ok: basketball score, FT, and player stat events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

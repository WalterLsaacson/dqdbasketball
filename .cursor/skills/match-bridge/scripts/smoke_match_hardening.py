#!/usr/bin/env python3
"""Smoke checks for basketball fixture matching and event enrichment."""

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


def main() -> int:
    _assert(
        bl.normalize_team("洛杉矶湖人") == bl.normalize_team("LA Lakers"),
        "Lakers CN ↔ abbreviation",
    )
    _assert(
        bl.normalize_team("纽约自由人") == bl.normalize_team("New York Liberty"),
        "WNBA alias",
    )
    _assert(
        bl.normalize_team("辽宁本钢") == bl.normalize_team("Liaoning Flying Leopards"),
        "CBA alias",
    )
    _assert(bl.normalize_league("美职篮") == "nba", "NBA alias")
    _assert(bl.normalize_league("美国女子职业篮球联赛") == "wnba", "WNBA alias")
    _assert(bl.normalize_league("中国男子篮球职业联赛") == "cba", "CBA alias")

    dqd = {
        "id": "nba-1",
        "home": "洛杉矶湖人",
        "away": "金州勇士",
        "league": "美职篮",
        "local_date": "2026-08-06",
        "time": "10:00",
    }
    pm = {
        "id": "pm-nba-1",
        "home": "Los Angeles Lakers",
        "away": "Golden State Warriors",
        "league": "NBA",
        "league_id": "nba",
        "kickoff_beijing": "2026-08-06 10:00",
        "market_refs": [{"category": "player_prop", "condition_id": "0xprop"}],
        "props": [{"player": "LeBron James", "stat": "points"}],
    }
    score = bl.score_pair(dqd, pm)
    _assert(score >= bl.DEFAULT_MIN_SCORE, f"NBA pair should match, got {score}")
    wrong_league = dict(pm, league="WNBA", league_id="wnba")
    _assert(bl.score_pair(dqd, wrong_league) == 0.0, "NBA must reject WNBA")

    paired = bl.match_fixtures([dqd], [pm], pm_stale_hours=-1)
    _assert(len(paired) == 1, "fixture should pair")
    events = bl.enrich_dqd_events(
        [
            {
                "type": "player_stat_change",
                "match_id": "nba-1",
                "person_id": "23",
                "person_en_name": "LeBron James",
                "changes": {"points": 3},
            },
            {"type": "ignored", "match_id": "nba-1"},
        ],
        paired,
    )
    _assert(len(events) == 1, "only supported event is forwarded")
    _assert(events[0]["polymarket"]["market_refs"], "market refs preserved")
    _assert(events[0]["polymarket"]["props"], "props preserved")
    print("smoke_match_hardening: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

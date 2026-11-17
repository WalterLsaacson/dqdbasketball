"""Each market family must settle from its own score bucket.

Regression cover for three real mispricings that were recorded live on
2026-11-09 with Polymarket officially resolving all of them LOSE while the
quote engine had marked them WIN at ask 0.19-0.20:

* threes props settled from points ("3-Pointers Made" contains "point")
* period spreads settled from the full-game margin
* "Team to Score First" settled from the full-game winner
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".cursor" / "skills" / "polymarket-quote" / "scripts"))

import quote_lib  # noqa: E402


def prop(question: str, smt: str, line: float, mid: str = "m1") -> dict:
    return {
        "question": question,
        "sports_market_type": smt,
        "outcomes": ["Yes", "No"],
        "clob_token_ids": [f"{mid}-yes", f"{mid}-no"],
        "market_id": mid,
        "line": line,
    }


class ThreesNeverSettleFromPoints(unittest.TestCase):
    """163 skipped buy_win legs: 141 threes were settled from points."""

    def test_threes_market_ignores_points_stat(self) -> None:
        markets = [prop("Jalen Smith: 3-Pointers Made O/U 2.5", "threes", 2.5)]
        # Jalen Smith scored 17 but made only 2 threes; official result LOSE.
        self.assertEqual(
            quote_lib.player_prop_tokens(
                markets, player="Jalen Smith", stat="score", curr=17, mode="ft"
            ),
            [],
        )
        self.assertEqual(
            quote_lib.player_prop_tokens(
                markets, player="Jalen Smith", stat="points", curr=17, mode="ft"
            ),
            [],
        )
        rows = quote_lib.player_prop_tokens(
            markets, player="Jalen Smith", stat="three_point", curr="2-5", mode="ft"
        )
        self.assertTrue(rows)
        by_outcome = {r["outcome"]: r["settlement"] for r in rows}
        # 2 made threes vs O/U 2.5 -> Over LOSE, Under WIN (matches Polymarket).
        self.assertEqual(by_outcome["Yes"], "LOSE")
        self.assertEqual(by_outcome["No"], "WIN")

    def test_points_market_still_uses_points(self) -> None:
        markets = [prop("Jalen Smith: Points O/U 14.5", "points", 14.5, "m2")]
        rows = quote_lib.player_prop_tokens(
            markets, player="Jalen Smith", stat="score", curr=17, mode="ft"
        )
        self.assertTrue(rows)
        self.assertEqual(rows[0]["settlement"], "WIN")

    def test_made_attempted_parse(self) -> None:
        self.assertEqual(quote_lib._stat_number("2-5"), 2.0)
        self.assertEqual(quote_lib._stat_number(0), 0.0)
        self.assertEqual(quote_lib._stat_number(""), None)

    def test_market_keys_are_unique_per_market(self) -> None:
        markets = [
            prop("Josh Giddey: 3-Pointers Made O/U 1.5", "threes", 1.5, "aaa111"),
            prop("Josh Giddey: 3-Pointers Made O/U 2.5", "threes", 2.5, "bbb222"),
        ]
        rows = quote_lib.player_prop_tokens(
            markets, player="Josh Giddey", stat="three_point", curr="1-4", mode="ft"
        )
        self.assertEqual(len({r["market_key"] for r in rows}), len(rows))


class PeriodSpreadUsesItsOwnQuarter(unittest.TestCase):
    """17 q4 + 2 half + 1 q1 spreads were settled from the game margin."""

    def market(self, question: str, smt: str, line: float) -> dict:
        return {
            "question": question,
            "sports_market_type": smt,
            "outcomes": ["76ers", "Bulls"],
            "clob_token_ids": ["philly", "chicago"],
            "market_id": "sp1",
            "line": line,
        }

    def test_q4_spread_uses_q4_only(self) -> None:
        # Bulls 121 - 127 76ers overall, but Q4 was 28-28, so -1.5 loses.
        m = self.market("4Q Spread: 76ers (-1.5)", "q4_spreads", -1.5)
        rows = quote_lib.spread_tokens(
            [m],
            home="Bulls",
            away="76ers",
            home_score=121,
            away_score=127,
            home_fs_4=[33, 28, 32, 28],
            away_fs_4=[34, 34, 31, 28],
        )
        by_outcome = {r["outcome"]: r["settlement"] for r in rows}
        self.assertEqual(by_outcome["76ers"], "LOSE")
        self.assertEqual(by_outcome["Bulls"], "WIN")
        self.assertEqual(rows[0]["period"], "4q")

    def test_q4_spread_without_quarters_is_skipped(self) -> None:
        m = self.market("4Q Spread: 76ers (-1.5)", "q4_spreads", -1.5)
        self.assertEqual(
            quote_lib.spread_tokens(
                [m], home="Bulls", away="76ers", home_score=121, away_score=127
            ),
            [],
        )

    def test_first_half_spread_uses_first_two_quarters(self) -> None:
        m = self.market("1H Spread: Mavericks (-20.5)", "first_half_spreads", -20.5)
        m["outcomes"] = ["Magic", "Mavericks"]
        rows = quote_lib.spread_tokens(
            [m],
            home="Magic",
            away="Mavericks",
            home_score=100,
            away_score=130,
            home_fs_4=[30, 30, 20, 20],
            away_fs_4=[25, 25, 40, 40],
        )
        by_outcome = {r["outcome"]: r["settlement"] for r in rows}
        # Mavericks trailed 60-50 at half: -20.5 loses despite the 30-point win.
        self.assertEqual(by_outcome["Mavericks"], "LOSE")
        self.assertEqual(by_outcome["Magic"], "WIN")
        self.assertEqual(rows[0]["period"], "1h")

    def test_full_game_spread_still_uses_final_margin(self) -> None:
        m = self.market("Spread: Mavericks (-2.5)", "spreads", -2.5)
        m["outcomes"] = ["Magic", "Mavericks"]
        rows = quote_lib.spread_tokens(
            [m], home="Magic", away="Mavericks", home_score=100, away_score=130
        )
        self.assertEqual(rows[0]["period"], "ft")
        by_outcome = {r["outcome"]: r["settlement"] for r in rows}
        # 100-130 final: Mavericks -2.5 covers comfortably.
        self.assertEqual(by_outcome["Mavericks"], "WIN")
        self.assertEqual(by_outcome["Magic"], "LOSE")


class ScoreFirstIsNotTheGameWinner(unittest.TestCase):
    """Celtics vs Bucks: Bucks scored first, Celtics won the game."""

    def market(self) -> dict:
        return {
            "question": "Celtics vs. Bucks: Team to Score First",
            "sports_market_type": "basketball_team_to_score_first",
            "outcomes": ["Celtics", "Bucks"],
            "clob_token_ids": ["bos", "mil"],
            "market_id": "sf1",
        }

    def test_score_first_uses_first_scoring_side(self) -> None:
        rows = quote_lib.first_scorer_tokens(
            [self.market()], home="Bucks", away="Celtics", first_side="home"
        )
        by_outcome = {r["outcome"]: r["settlement"] for r in rows}
        self.assertEqual(by_outcome["Bucks"], "WIN")
        self.assertEqual(by_outcome["Celtics"], "LOSE")

    def test_score_first_without_first_side_pends(self) -> None:
        rows = quote_lib.first_scorer_tokens(
            [self.market()], home="Bucks", away="Celtics", first_side=None
        )
        self.assertTrue(rows)
        self.assertTrue(all(r["settlement"] == "PENDING" for r in rows))

    def test_moneyline_skips_sibling_market_types(self) -> None:
        for smt in (
            "basketball_team_to_score_first",
            "first_half_moneyline",
            "basketball_game_race_to_points",
        ):
            m = dict(self.market(), sports_market_type=smt)
            self.assertEqual(
                quote_lib.moneyline_tokens(
                    [m], home="Bucks", away="Celtics", home_score=97, away_score=117
                ),
                [],
                smt,
            )

    def test_moneyline_still_settles_the_winner(self) -> None:
        m = {
            "question": "Celtics vs. Bucks",
            "sports_market_type": "moneyline",
            "outcomes": ["Bucks", "Celtics"],
            "clob_token_ids": ["mil", "bos"],
        }
        rows = quote_lib.moneyline_tokens(
            [m], home="Bucks", away="Celtics", home_score=97, away_score=117
        )
        by_outcome = {r["outcome"]: r["settlement"] for r in rows}
        self.assertEqual(by_outcome["Celtics"], "WIN")
        self.assertEqual(by_outcome["Bucks"], "LOSE")


class CollectTargetTokensSmoke(unittest.TestCase):
    """The FT path must collect every family without raising."""

    def test_ft_collection_includes_each_family(self) -> None:
        markets = [
            {
                "market_id": "ml1",
                "question": "Celtics vs. Bucks",
                "sports_market_type": "moneyline",
                "outcomes": ["Bucks", "Celtics"],
                "clob_token_ids": ["mil", "bos"],
            },
            {
                "market_id": "sf1",
                "question": "Celtics vs. Bucks: Team to Score First",
                "sports_market_type": "basketball_team_to_score_first",
                "outcomes": ["Bucks", "Celtics"],
                "clob_token_ids": ["sf-mil", "sf-bos"],
            },
            {
                "market_id": "sp1",
                "question": "4Q Spread: Celtics (-1.5)",
                "sports_market_type": "q4_spreads",
                "outcomes": ["Celtics", "Bucks"],
                "clob_token_ids": ["sp-bos", "sp-mil"],
                "line": -1.5,
            },
            {
                "market_id": "p1",
                "question": "Jayson Tatum: 3-Pointers Made O/U 2.5",
                "sports_market_type": "threes",
                "outcomes": ["Yes", "No"],
                "clob_token_ids": ["p-yes", "p-no"],
                "line": 2.5,
            },
        ]
        rows = []
        rows.extend(
            quote_lib.moneyline_tokens(
                markets, home="Bucks", away="Celtics", home_score=97, away_score=117
            )
        )
        rows.extend(
            quote_lib.first_scorer_tokens(
                markets, home="Bucks", away="Celtics", first_side="home"
            )
        )
        rows.extend(
            quote_lib.spread_tokens(
                markets,
                home="Bucks",
                away="Celtics",
                home_score=97,
                away_score=117,
                home_fs_4=[20, 20, 20, 37],
                away_fs_4=[30, 30, 30, 27],
            )
        )
        rows.extend(
            quote_lib.player_prop_tokens(
                markets, player="Jayson Tatum", stat="three_point", curr="3-7", mode="ft"
            )
        )
        families = {r["family"] for r in rows}
        self.assertEqual(families, {"moneyline", "spreads", "player_prop"})
        # Every token must be unique so idempotency keys do not collide.
        tokens = [r["token_id"] for r in rows]
        self.assertEqual(len(tokens), len(set(tokens)))


class FinishedEventEndToEnd(unittest.TestCase):
    """quote_finished_event must not raise while building FT token rows.

    A NameError here would abort every match_finished quote in production, so
    the whole path is exercised with network calls stubbed out.
    """

    MARKETS = [
        {
            "market_id": "ml1",
            "question": "Celtics vs. Bucks",
            "sports_market_type": "moneyline",
            "outcomes": ["Bucks", "Celtics"],
            "clob_token_ids": ["mil", "bos"],
        },
        {
            "market_id": "sf1",
            "question": "Celtics vs. Bucks: Team to Score First",
            "sports_market_type": "basketball_team_to_score_first",
            "outcomes": ["Bucks", "Celtics"],
            "clob_token_ids": ["sf-mil", "sf-bos"],
        },
        {
            "market_id": "sp1",
            "question": "4Q Spread: Celtics (-1.5)",
            "sports_market_type": "q4_spreads",
            "outcomes": ["Celtics", "Bucks"],
            "clob_token_ids": ["sp-bos", "sp-mil"],
            "line": -1.5,
        },
        {
            "market_id": "p1",
            "question": "Jayson Tatum: 3-Pointers Made O/U 2.5",
            "sports_market_type": "threes",
            "outcomes": ["Yes", "No"],
            "clob_token_ids": ["p-yes", "p-no"],
            "line": 2.5,
        },
    ]

    def test_finished_event_quotes_without_error(self) -> None:
        from unittest import mock

        captured: dict[str, list] = {}

        def fake_collect(ctx, **kwargs):
            captured["tokens"], meta = self._rows(kwargs.get("root"))
            return captured["tokens"], meta

        def fake_quote_tokens(rows, **kwargs):
            return [dict(r, best_bid=0.98, best_ask=0.99, misprice=False) for r in rows]

        ev = {
            "type": "match_finished",
            "match_id": "54556372",
            "home": "Bucks",
            "away": "Celtics",
            "home_score": 97,
            "away_score": 117,
            "polymarket": {"event_id": "1", "slug": "x"},
        }
        ctx = {
            "event": ev,
            "match_row": None,
            "dongqiudi": {"id": "54556372", "home_fs_4": [20, 20, 20, 37],
                          "away_fs_4": [30, 30, 30, 27]},
            "polymarket": {"event_id": "1", "slug": "x", "props": [], "market_refs": []},
            "home_score": 97,
            "away_score": 117,
            "home": "Bucks",
            "away": "Celtics",
        }
        with mock.patch.object(quote_lib, "join_ft_context", return_value=ctx), \
             mock.patch.object(quote_lib, "collect_target_tokens", side_effect=fake_collect), \
             mock.patch.object(quote_lib, "quote_tokens", side_effect=fake_quote_tokens):
            bundle = quote_lib.quote_finished_event(
                ROOT, ev, persist=False, include_exact=False
            )
        self.assertIn("quotes", bundle)
        # LOSE legs are dropped before quoting, so count <= collected rows.
        tradeable = [
            r for r in captured["tokens"] if str(r.get("settlement") or "").upper() != "LOSE"
        ]
        self.assertEqual(bundle["count"], len(tradeable))
        self.assertGreater(bundle["count"], 0)

    @classmethod
    def _rows(cls, root):
        rows = []
        rows.extend(
            quote_lib.moneyline_tokens(
                cls.MARKETS, home="Bucks", away="Celtics", home_score=97, away_score=117
            )
        )
        rows.extend(
            quote_lib.first_scorer_tokens(
                cls.MARKETS, home="Bucks", away="Celtics", first_side="home"
            )
        )
        rows.extend(
            quote_lib.spread_tokens(
                cls.MARKETS,
                home="Bucks",
                away="Celtics",
                home_score=97,
                away_score=117,
                home_fs_4=[20, 20, 20, 37],
                away_fs_4=[30, 30, 30, 27],
            )
        )
        rows.extend(
            quote_lib.player_prop_tokens(
                cls.MARKETS, player="Jayson Tatum", stat="three_point", curr="3-7", mode="ft"
            )
        )
        return rows, {"mode": "ft", "skipped": []}


if __name__ == "__main__":
    unittest.main()

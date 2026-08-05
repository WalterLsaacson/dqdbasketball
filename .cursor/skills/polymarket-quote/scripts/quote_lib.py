#!/usr/bin/env python3
"""Basketball Polymarket quoting: settle trusted DQD bridge events and CLOB books."""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TZ_CN = timezone(timedelta(hours=8))
CLOB_HOST = "clob.polymarket.com"
CLOB_BASE = f"https://{CLOB_HOST}"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Import Gamma helpers from the basketball Polymarket skill.
_PM_SCRIPTS = Path(__file__).resolve().parents[2] / "polymarket-basketball" / "scripts"
if str(_PM_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PM_SCRIPTS))
import pm_lib as pm  # noqa: E402

WIN_RE = re.compile(r"will\s+(.+?)\s+win\s+on\b", re.I)
DRAW_RE = re.compile(r"end\s+in\s+a\s+draw", re.I)
SPREAD_RE = re.compile(r"spread:\s*(.+?)\s*\(([+-]?\d+(?:\.\d+)?)\)", re.I)
TOTAL_RE = re.compile(r"o/?u\s*(\d+(?:\.\d+)?)", re.I)
HALF_1_RE = re.compile(r"(?:1st|first)\s*half", re.I)
HALF_2_RE = re.compile(r"(?:2nd|second)\s*half", re.I)
EXACT_WIN_RE = re.compile(r"will\s+(.+?)\s+win\s+(\d+)\s*[-–]\s*(\d+)\s*\?", re.I)
EXACT_DRAW_RE = re.compile(r"draw\s+(\d+)\s*[-–]\s*(\d+)", re.I)
SCORE_PAIR_RE = re.compile(r"\b(\d+)\s*[-–]\s*(\d+)\b")

DEFAULT_EPS = 0.005
# Polymarket sports taker feeRate (2026): fee/share = feeRate * p * (1-p)
SPORTS_TAKER_FEE_RATE = 0.05
# Min net edge per share after fee before writing opportunities.jsonl
DEFAULT_MIN_NET = 0.02
TOP_N = 5


class QuoteError(RuntimeError):
    pass


def taker_fee_per_share(price: float, fee_rate: float = SPORTS_TAKER_FEE_RATE) -> float:
    """USDC fee per share for a taker fill at `price` (0..1)."""
    p = max(0.0, min(1.0, float(price)))
    return float(fee_rate) * p * (1.0 - p)


def repo_root_from(scripts_file: Path) -> Path:
    # scripts -> polymarket-quote -> skills -> .cursor -> repo
    return scripts_file.resolve().parents[4]


def data_dir(root: Path) -> Path:
    return root / "data" / "pm-quote"


def bridge_dir(root: Path) -> Path:
    return root / "data" / "bridge"


def now_cn_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Append JSONL under exclusive flock (pairs with data_prune.prune_jsonl)."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            with path.open("a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


_PERSIST_EXEC: ThreadPoolExecutor | None = None
_PERSIST_LOCK = threading.Lock()


def _persist_executor() -> ThreadPoolExecutor:
    global _PERSIST_EXEC
    with _PERSIST_LOCK:
        if _PERSIST_EXEC is None:
            _PERSIST_EXEC = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="quote-persist"
            )
        return _PERSIST_EXEC


def append_jsonl_async(path: Path, rows: list[dict[str, Any]]) -> None:
    """Fire-and-forget JSONL append (does not block the trade hot path)."""
    if not rows:
        return
    _persist_executor().submit(append_jsonl, path, list(rows))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Limited concurrency for independent FAK posts within one quote phase.
TRADE_POOL_WORKERS = max(1, int(os.getenv("QUOTE_TRADE_WORKERS", "2") or 2))
_TRADE_EXEC: ThreadPoolExecutor | None = None
_TRADE_EXEC_LOCK = threading.Lock()


def _trade_executor_pool() -> ThreadPoolExecutor:
    global _TRADE_EXEC
    with _TRADE_EXEC_LOCK:
        if _TRADE_EXEC is None:
            _TRADE_EXEC = ThreadPoolExecutor(
                max_workers=TRADE_POOL_WORKERS,
                thread_name_prefix="quote-trade",
            )
        return _TRADE_EXEC


# DQD-score CLOB quote while AF confirms (no trade). Warms books + logs early edge.
PRECONFIRM_POOL_WORKERS = max(1, int(os.getenv("QUOTE_PRECONFIRM_WORKERS", "4") or 4))
_PRECONFIRM_EXEC: ThreadPoolExecutor | None = None
_PRECONFIRM_LOCK = threading.Lock()
_PRECONFIRM_FUTS: dict[str, Any] = {}


def _preconfirm_executor() -> ThreadPoolExecutor:
    global _PRECONFIRM_EXEC
    with _PRECONFIRM_LOCK:
        if _PRECONFIRM_EXEC is None:
            _PRECONFIRM_EXEC = ThreadPoolExecutor(
                max_workers=PRECONFIRM_POOL_WORKERS,
                thread_name_prefix="af-preconfirm",
            )
        return _PRECONFIRM_EXEC


def _preconfirm_summary(bundle: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(bundle, dict):
        return None
    opps = bundle.get("opportunities") or []
    best = None
    for o in opps:
        if not isinstance(o, dict):
            continue
        edge = o.get("net_edge")
        if edge is None:
            continue
        try:
            e = float(edge)
        except (TypeError, ValueError):
            continue
        if best is None or e > best:
            best = e
    return {
        "quoted_at": bundle.get("quoted_at"),
        "opportunity_count": int(bundle.get("opportunity_count") or len(opps) or 0),
        "best_net_edge": best,
        "error": bundle.get("error"),
    }

def _parse_list_field(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            val = json.loads(s)
            return val if isinstance(val, list) else [val]
        except json.JSONDecodeError:
            return [s]
    return []


def enrich_market(m: dict[str, Any]) -> dict[str, Any]:
    outcomes = [str(x) for x in _parse_list_field(m.get("outcomes"))]
    tokens = [str(t) for t in _parse_list_field(m.get("clobTokenIds") or m.get("clob_token_ids")) if t]
    return {
        "market_id": str(m.get("id") or m.get("conditionId") or "").strip(),
        "condition_id": str(m.get("conditionId") or m.get("condition_id") or "").strip(),
        "question": str(m.get("question") or m.get("groupItemTitle") or ""),
        "group_item_title": str(m.get("groupItemTitle") or m.get("group_item_title") or ""),
        "slug": str(m.get("slug") or ""),
        "sports_market_type": str(m.get("sportsMarketType") or m.get("sports_market_type") or ""),
        "outcomes": outcomes,
        "clob_token_ids": tokens,
        "neg_risk": bool(m.get("negRisk") if m.get("negRisk") is not None else m.get("neg_risk")),
        "active": bool(m.get("active", True)),
        "closed": bool(m.get("closed", False)),
    }


def fetch_gamma_event(
    *,
    event_id: str | None = None,
    slug: str | None = None,
    proxy: str | None | object = ...,
) -> dict[str, Any] | None:
    if event_id:
        data = pm.fetch_json(f"/events/{event_id}", proxy=proxy)
        if isinstance(data, list) and data:
            return data[0] if isinstance(data[0], dict) else None
        return data if isinstance(data, dict) else None
    if slug:
        data = pm.fetch_json("/events", {"slug": slug}, proxy=proxy)
        if isinstance(data, list) and data:
            return data[0] if isinstance(data[0], dict) else None
        if isinstance(data, dict):
            return data
    return None


def discover_related_events(
    *,
    slug: str,
    home: str,
    away: str,
    proxy: str | None | object = ...,
) -> dict[str, dict[str, Any] | None]:
    """Fetch sibling More Markets / Exact Score events when present."""
    out: dict[str, dict[str, Any] | None] = {"more_markets": None, "exact_score": None}
    candidates = [
        ("more_markets", f"{slug}-more-markets"),
        ("exact_score", f"{slug}-exact-score"),
    ]
    # Title-based slugs vary; also try common patterns.
    home_slug = re.sub(r"[^a-z0-9]+", "-", home.lower()).strip("-")
    away_slug = re.sub(r"[^a-z0-9]+", "-", away.lower()).strip("-")
    if home_slug and away_slug:
        candidates.append(("exact_score", f"{home_slug}-vs-{away_slug}-exact-score"))

    seen: set[str] = set()
    for key, s in candidates:
        if not s or s in seen:
            continue
        seen.add(s)
        try:
            ev = fetch_gamma_event(slug=s, proxy=proxy)
        except pm.FetchError:
            ev = None
        if ev and isinstance(ev, dict) and (ev.get("markets") or ev.get("id")):
            # Keep first hit per key.
            if out.get(key) is None:
                out[key] = ev
    return out


# --- Settlement -------------------------------------------------------------


def winner_from_score(home_score: Any, away_score: Any) -> str | None:
    try:
        h = int(home_score)
        a = int(away_score)
    except (TypeError, ValueError):
        return None
    if h > a:
        return "home"
    if h < a:
        return "away"
    return "draw"


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def classify_moneyline_role(
    market: dict[str, Any],
    home: str,
    away: str,
) -> str | None:
    q = market.get("question") or ""
    title = market.get("group_item_title") or ""
    blob = f"{q} {title}"
    if DRAW_RE.search(blob) or _norm_name(title) == "draw" or "draw" in _norm_name(title):
        return "draw"
    m = WIN_RE.search(q)
    team = (m.group(1) if m else title).strip()
    nt = _norm_name(team)
    nh, na = _norm_name(home), _norm_name(away)
    if nh and (nt == nh or nh in nt or nt in nh):
        return "home"
    if na and (nt == na or na in nt or nt in na):
        return "away"
    # Fallback: question contains team names
    if nh and nh in _norm_name(q) and "win" in q.lower():
        return "home"
    if na and na in _norm_name(q) and "win" in q.lower():
        return "away"
    return None


def settle_yes_no(yes_wins: bool) -> tuple[str, str]:
    """Return (yes_settlement, no_settlement)."""
    if yes_wins:
        return "WIN", "LOSE"
    return "LOSE", "WIN"


def moneyline_tokens(
    markets: list[dict[str, Any]],
    *,
    home: str,
    away: str,
    home_score: Any,
    away_score: Any,
) -> list[dict[str, Any]]:
    """Settle basketball moneyline tokens.

    Supports:
    - Two-way team-name markets: outcomes=["Home Team", "Away Team"]
    - Legacy Yes/No per-team markets ("Will X win…")
    """
    winner = winner_from_score(home_score, away_score)
    rows: list[dict[str, Any]] = []
    nh, na = _norm_name(home), _norm_name(away)

    def _team_role(label: str) -> str | None:
        nt = _norm_name(label)
        if not nt or nt in ("yes", "no", "draw"):
            return None
        if nh and (nt == nh or nh in nt or nt in nh):
            return "home"
        if na and (nt == na or na in nt or nt in na):
            return "away"
        return None

    for m in markets:
        smt = (m.get("sports_market_type") or "").lower()
        q = m.get("question") or ""
        if smt and smt not in ("moneyline", "child_moneyline", ""):
            if " vs" not in q.lower() and not WIN_RE.search(q):
                continue
        outcomes = list(m.get("outcomes") or [])
        tokens = list(m.get("clob_token_ids") or [])
        if len(tokens) < 2:
            continue

        # Prefer two-way team-name markets (current Gamma basketball shape).
        if len(outcomes) >= 2:
            r0, r1 = _team_role(str(outcomes[0])), _team_role(str(outcomes[1]))
            if r0 in ("home", "away") and r1 in ("home", "away") and r0 != r1:
                for i, role in ((0, r0), (1, r1)):
                    if winner is None:
                        settlement = "PENDING"
                    else:
                        settlement = "WIN" if role == winner else "LOSE"
                    rows.append(
                        {
                            "family": "moneyline",
                            "market_key": f"{role}_yes",
                            "role": role,
                            "outcome": str(outcomes[i]),
                            "settlement": settlement,
                            "token_id": tokens[i],
                            "market_id": m.get("market_id") or "",
                            "condition_id": m.get("condition_id") or "",
                            "question": q,
                            "sports_market_type": m.get("sports_market_type") or "moneyline",
                        }
                    )
                continue

        # Legacy Yes/No moneyline markets.
        role = classify_moneyline_role(m, home, away)
        if role not in ("home", "away"):
            continue
        yes_i, no_i = 0, 1
        for i, o in enumerate(outcomes):
            if str(o).lower() == "yes":
                yes_i = i
            elif str(o).lower() == "no":
                no_i = i
        if winner is None:
            yes_s = no_s = "PENDING"
        else:
            yes_wins = role == winner
            yes_s, no_s = settle_yes_no(yes_wins)
        for outcome, idx, settlement, suffix in (
            ("Yes", yes_i, yes_s, "yes"),
            ("No", no_i, no_s, "no"),
        ):
            if idx >= len(tokens):
                continue
            rows.append(
                {
                    "family": "moneyline",
                    "market_key": f"{role}_{suffix}",
                    "role": role,
                    "outcome": outcome,
                    "settlement": settlement,
                    "token_id": tokens[idx],
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": q,
                    "sports_market_type": m.get("sports_market_type") or "moneyline",
                }
            )
    order = ["home_yes", "home_no", "away_yes", "away_no"]
    rows.sort(key=lambda r: order.index(r["market_key"]) if r["market_key"] in order else 99)
    return rows


def _outcome_is_over(label: str) -> bool | None:
    """Map Over/Under/Yes/No labels. Yes=Over, No=Under for basketball props."""
    n = (label or "").strip().lower()
    if not n:
        return None
    if n in ("over", "yes", "o") or n.startswith("over"):
        return True
    if n in ("under", "no", "u") or n.startswith("under"):
        return False
    return None


_PLAYER_STAT_ALIASES = {
    "score": ("score", "points", "point", "pts"),
    "points": ("points", "point", "pts", "score"),
    "assists": ("assists", "assist", "ast"),
    "assist": ("assists", "assist", "ast"),
    "rebounds": ("rebounds", "rebound", "reb"),
    "rebound": ("rebounds", "rebound", "reb"),
    "steals": ("steals", "steal", "stl"),
    "steal": ("steals", "steal", "stl"),
    "blocks": ("blocks", "block", "blk"),
    "block": ("blocks", "block", "blk"),
    "threes": ("threes", "three", "3pt", "3-pointers", "three_point"),
    "three_point": ("threes", "three", "3pt", "3-pointers", "three_point"),
}


def player_prop_tokens(
    markets: list[dict[str, Any]],
    *,
    player: str,
    stat: str,
    curr: Any,
    mode: str = "live",
) -> list[dict[str, Any]]:
    """Settle supported basketball player O/U tokens from a DQD stat event.

    Gamma basketball props commonly use Yes/No (Yes=Over). During live play only
    an already-cleared Over is tradeable; Under waits until FT.
    """
    try:
        value = float(curr)
    except (TypeError, ValueError):
        return []
    wanted = _norm_name(stat)
    aliases = _PLAYER_STAT_ALIASES.get(wanted, (wanted,))
    player_key = _norm_name(player)
    rows: list[dict[str, Any]] = []
    for m in markets:
        q = str(m.get("question") or "")
        blob = _norm_name(f"{m.get('sports_market_type') or ''} {q}")
        if not any(_norm_name(alias) in blob for alias in aliases):
            continue
        if player_key and player_key not in blob:
            continue
        line_match = re.search(r"\b(?:over|under|o/u)\s+(-?\d+(?:\.\d+)?)\b", q, re.I)
        if not line_match:
            line_match = re.search(r"(-?\d+(?:\.\d+)?)\s*$", q)
        raw_line = m.get("line") or m.get("handicap") or m.get("spread")
        try:
            line = float(raw_line if raw_line is not None else line_match.group(1))
        except (AttributeError, TypeError, ValueError):
            continue
        over_wins = value > line
        if mode == "live" and not over_wins:
            continue
        outcomes = m.get("outcomes") or ["Yes", "No"]
        for i, token in enumerate((m.get("clob_token_ids") or [])[:2]):
            label = str(outcomes[i]) if i < len(outcomes) else f"outcome_{i}"
            is_over = _outcome_is_over(label)
            if is_over is None:
                # Fall back: index 0 = Over/Yes for standard two-outcome props.
                is_over = i == 0
            settlement = "WIN" if (is_over and over_wins) or ((not is_over) and not over_wins) else "LOSE"
            if mode == "live" and not is_over:
                continue
            rows.append(
                {
                    "family": "player_prop",
                    "market_key": f"player_{player_key}_{wanted}_{line}_{'over' if is_over else 'under'}",
                    "role": "player_prop",
                    "outcome": label,
                    "settlement": settlement,
                    "token_id": str(token),
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": q,
                    "sports_market_type": m.get("sports_market_type") or "player_prop",
                    "player": player,
                    "stat": stat,
                    "line": line,
                    "curr": value,
                }
            )
    return rows


def _parse_spread(market: dict[str, Any]) -> tuple[str, float] | None:
    q = market.get("question") or ""
    m = SPREAD_RE.search(q)
    if m:
        return m.group(1).strip(), float(m.group(2))
    smt = (market.get("sports_market_type") or "").lower()
    if smt != "spreads":
        return None
    # line sometimes in group title
    m2 = re.search(r"\(([+-]?\d+(?:\.\d+)?)\)", q)
    team = (market.get("group_item_title") or "").strip() or q
    if m2:
        return team, float(m2.group(1))
    return None


def spread_tokens(
    markets: list[dict[str, Any]],
    *,
    home: str,
    away: str,
    home_score: Any,
    away_score: Any,
) -> list[dict[str, Any]]:
    try:
        h, a = int(home_score), int(away_score)
    except (TypeError, ValueError):
        return []
    margin = h - a
    rows: list[dict[str, Any]] = []
    for m in markets:
        smt = (m.get("sports_market_type") or "").lower()
        parsed = _parse_spread(m)
        if smt not in ("spreads", "spread") and not parsed:
            if "spread:" not in (m.get("question") or "").lower():
                continue
        if not parsed:
            continue
        fav_team, line = parsed
        # Handicap applies to favorite team named in "Spread: Team (line)"
        # If line is -1.5 on home, home covers when margin > 1.5
        fav_is_home = _norm_name(fav_team) == _norm_name(home) or _norm_name(home) in _norm_name(
            fav_team
        )
        fav_margin = margin if fav_is_home else -margin
        fav_covers = fav_margin + line > 0
        outcomes = m.get("outcomes") or []
        tokens = m.get("clob_token_ids") or []
        if len(tokens) < 2 or len(outcomes) < 2:
            # assume [favorite, underdog] or Yes/No
            outcomes = outcomes or [fav_team, away if fav_is_home else home]
        for i, token in enumerate(tokens[:2]):
            label = str(outcomes[i]) if i < len(outcomes) else f"outcome_{i}"
            nl = _norm_name(label)
            is_fav = nl == _norm_name(fav_team) or _norm_name(fav_team) in nl or label.lower() == "yes"
            if label.lower() == "no":
                is_fav = False
            settlement = "WIN" if (is_fav and fav_covers) or ((not is_fav) and (not fav_covers)) else "LOSE"
            rows.append(
                {
                    "family": "spreads",
                    "market_key": f"spread_{_norm_name(fav_team)}_{line}_{i}",
                    "role": "spread",
                    "outcome": label,
                    "settlement": settlement,
                    "token_id": token,
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": m.get("question") or "",
                    "sports_market_type": "spreads",
                    "line": line,
                    "favorite": fav_team,
                }
            )
    return rows


def _parse_int_score(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_total_market(
    question: str,
    home: str,
    away: str,
) -> dict[str, Any] | None:
    """Classify O/U markets: match vs team, and ft / 1h / 2h.

    Examples:
      "A vs. B: O/U 2.5" → match ft
      "A vs. B: 1st Half O/U 0.5" → match 1h
      "A vs. B: CS Huancayo O/U 2.5" → away ft
      "A vs. B: Spain 1st Half O/U 0.5" → home 1h
    """
    q = question or ""
    mline = TOTAL_RE.search(q)
    if not mline:
        return None
    line = float(mline.group(1))
    period = "ft"
    if HALF_1_RE.search(q):
        period = "1h"
    elif HALF_2_RE.search(q):
        period = "2h"

    suffix = q.split(":")[-1].strip() if ":" in q else q
    team_blob = TOTAL_RE.sub("", suffix)
    team_blob = HALF_1_RE.sub("", team_blob)
    team_blob = HALF_2_RE.sub("", team_blob)
    team_blob = team_blob.strip(" :-")

    side = "match"
    if team_blob:
        nt = _norm_name(team_blob)
        nh, na = _norm_name(home), _norm_name(away)
        if nh and (nt == nh or nh in nt or nt in nh):
            side = "home"
        elif na and (nt == na or na in nt or nt in na):
            side = "away"
        else:
            # Named team we cannot map — do not fall back to match total.
            return None

    return {"line": line, "period": period, "side": side}


def _goals_for_total(
    *,
    side: str,
    period: str,
    home_score: Any,
    away_score: Any,
    home_half: Any = None,
    away_half: Any = None,
) -> int | None:
    """Return the goal count that settles this total, or None if unknown."""
    h = _parse_int_score(home_score)
    a = _parse_int_score(away_score)
    if h is None or a is None:
        return None
    hh = _parse_int_score(home_half)
    ah = _parse_int_score(away_half)

    if period == "ft":
        if side == "match":
            return h + a
        if side == "home":
            return h
        if side == "away":
            return a
        return None

    if period == "1h":
        if hh is None or ah is None:
            return None
        if side == "match":
            return hh + ah
        if side == "home":
            return hh
        if side == "away":
            return ah
        return None

    if period == "2h":
        if hh is None or ah is None:
            return None
        h2, a2 = h - hh, a - ah
        if h2 < 0 or a2 < 0:
            return None
        if side == "match":
            return h2 + a2
        if side == "home":
            return h2
        if side == "away":
            return a2
        return None

    return None


def totals_tokens(
    markets: list[dict[str, Any]],
    *,
    home: str,
    away: str,
    home_score: Any,
    away_score: Any,
    home_half: Any = None,
    away_half: Any = None,
    mode: str = "ft",
) -> list[dict[str, Any]]:
    """Settle match / team / half O/U tokens using the correct goal bucket."""
    rows: list[dict[str, Any]] = []
    for m in markets:
        smt = (m.get("sports_market_type") or "").lower()
        q = m.get("question") or ""
        parsed = _parse_total_market(q, home, away)
        if smt not in ("totals", "total", "team_totals", "team_total") and not parsed:
            continue
        if not parsed:
            continue
        line = float(parsed["line"])
        side = str(parsed["side"])
        period = str(parsed["period"])
        goals = _goals_for_total(
            side=side,
            period=period,
            home_score=home_score,
            away_score=away_score,
            home_half=home_half,
            away_half=away_half,
        )
        if goals is None:
            continue
        # Live: only emit when Over is already locked (goals can only rise).
        if mode == "live" and not (goals > line):
            continue
        over_wins = goals > line
        outcomes = m.get("outcomes") or ["Over", "Under"]
        tokens = m.get("clob_token_ids") or []
        side_key = "match" if side == "match" else side
        period_key = "" if period == "ft" else f"_{period}"
        for i, token in enumerate(tokens[:2]):
            label = str(outcomes[i]) if i < len(outcomes) else f"outcome_{i}"
            is_over = _outcome_is_over(label)
            if is_over is None:
                is_over = "over" in label.lower() or i == 0
            settlement = (
                "WIN" if (is_over and over_wins) or ((not is_over) and (not over_wins)) else "LOSE"
            )
            # Live: only trade the locked Over leg.
            if mode == "live" and not is_over:
                continue
            rows.append(
                {
                    "family": "totals",
                    "market_key": f"{side_key}{period_key}_total_{line}_{'over' if is_over else 'under'}",
                    "role": "totals",
                    "outcome": label,
                    "settlement": settlement,
                    "locked": True,
                    "token_id": token,
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": q,
                    "sports_market_type": "totals",
                    "line": line,
                    "goals": goals,
                    "total_side": side,
                    "total_period": period,
                }
            )
    return rows


def _parse_btts_period(question: str) -> str:
    """Return ft | 1h | 2h for BTTS markets."""
    q = question or ""
    if HALF_2_RE.search(q) or re.search(r"in\s+second\s+half", q, re.I):
        return "2h"
    if HALF_1_RE.search(q) or re.search(r"in\s+(?:the\s+)?(?:1st|first)\s+half", q, re.I):
        return "1h"
    return "ft"


def _btts_both_scored(
    *,
    period: str,
    home_score: Any,
    away_score: Any,
    home_half: Any = None,
    away_half: Any = None,
) -> bool | None:
    """True/False if decidable; None if half scores required but missing."""
    h = _parse_int_score(home_score)
    a = _parse_int_score(away_score)
    if h is None or a is None:
        return None
    if period == "ft":
        return h > 0 and a > 0

    hh = _parse_int_score(home_half)
    ah = _parse_int_score(away_half)
    if hh is None or ah is None:
        return None

    if period == "1h":
        return hh > 0 and ah > 0

    if period == "2h":
        h2, a2 = h - hh, a - ah
        if h2 < 0 or a2 < 0:
            return None
        return h2 > 0 and a2 > 0

    return None


def btts_tokens(
    markets: list[dict[str, Any]],
    *,
    home_score: Any,
    away_score: Any,
    home_half: Any = None,
    away_half: Any = None,
    mode: str = "ft",
) -> list[dict[str, Any]]:
    """Settle BTTS Yes/No for full match, 1st half, or 2nd half.

    Half markets require `home_half` / `away_half`. Without them we skip — never
    fall back to full-time BTTS (that caused false sell_lose on 2H No).
    Live: only emit once both sides have scored in that period (Yes locked).
    """
    rows: list[dict[str, Any]] = []
    for m in markets:
        smt = (m.get("sports_market_type") or "").lower()
        q_raw = m.get("question") or ""
        q = q_raw.lower()
        if smt not in ("both_teams_to_score", "btts") and "both teams to score" not in q:
            continue
        period = _parse_btts_period(q_raw)
        both = _btts_both_scored(
            period=period,
            home_score=home_score,
            away_score=away_score,
            home_half=home_half,
            away_half=away_half,
        )
        if both is None:
            continue
        # Live: only lock after both have scored in-period (No is then dead).
        if mode == "live" and not both:
            continue
        outcomes = m.get("outcomes") or ["Yes", "No"]
        tokens = m.get("clob_token_ids") or []
        period_key = "" if period == "ft" else f"_{period}"
        for i, token in enumerate(tokens[:2]):
            label = str(outcomes[i]) if i < len(outcomes) else f"outcome_{i}"
            is_yes = label.lower() == "yes"
            settlement = "WIN" if (is_yes and both) or ((not is_yes) and (not both)) else "LOSE"
            rows.append(
                {
                    "family": "btts",
                    "market_key": f"btts{period_key}_{'yes' if is_yes else 'no'}",
                    "role": "btts",
                    "outcome": label,
                    "settlement": settlement,
                    "locked": True,
                    "token_id": token,
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": q_raw,
                    "sports_market_type": "both_teams_to_score",
                    "btts_period": period,
                    "btts_both": both,
                }
            )
    return rows


def exact_score_tokens(
    markets: list[dict[str, Any]],
    *,
    home: str,
    away: str,
    home_score: Any,
    away_score: Any,
    mode: str = "ft",
) -> list[dict[str, Any]]:
    """Settle exact-score Yes/No tokens.

    FT: only the final H-A Yes is WIN.
    Live (soccer goals only rise): any scoreline with home<curr or away<curr is
    already dead → Yes=LOSE / No=WIN (e.g. after 1-0, 0-0 No is the locked buy).
    Still-possible scorelines are skipped in live mode (not locked yet).
    """
    try:
        h, a = int(home_score), int(away_score)
    except (TypeError, ValueError):
        return []
    target = f"{h}-{a}"
    rows: list[dict[str, Any]] = []
    for m in markets:
        q = m.get("question") or ""
        score = None
        mw = EXACT_WIN_RE.search(q)
        md = EXACT_DRAW_RE.search(q)
        if mw:
            score = f"{mw.group(2)}-{mw.group(3)}"
        elif md:
            score = f"{md.group(1)}-{md.group(2)}"
        else:
            ms = SCORE_PAIR_RE.search(q)
            if ms:
                score = f"{ms.group(1)}-{ms.group(2)}"
        if score is None:
            continue
        try:
            sh, sa = (int(x) for x in score.split("-", 1))
        except ValueError:
            continue

        if mode == "live":
            # Impossible once either side has already exceeded the printed scoreline.
            if sh < h or sa < a:
                yes_wins = False
                locked = True
            else:
                continue  # still reachable — wait for FT or further goals
        else:
            yes_wins = score == target
            locked = True

        outcomes = m.get("outcomes") or ["Yes", "No"]
        tokens = m.get("clob_token_ids") or []
        yes_i, no_i = 0, 1
        for i, o in enumerate(outcomes):
            if str(o).lower() == "yes":
                yes_i = i
            elif str(o).lower() == "no":
                no_i = i
        for outcome, idx, settlement in (
            ("Yes", yes_i, "WIN" if yes_wins else "LOSE"),
            ("No", no_i, "LOSE" if yes_wins else "WIN"),
        ):
            if idx >= len(tokens):
                continue
            rows.append(
                {
                    "family": "exact_score",
                    "market_key": f"exact_{score}_{outcome.lower()}",
                    "role": "exact_score",
                    "outcome": outcome,
                    "settlement": settlement,
                    "locked": locked,
                    "token_id": tokens[idx],
                    "market_id": m.get("market_id") or "",
                    "condition_id": m.get("condition_id") or "",
                    "question": q,
                    "sports_market_type": "exact_score",
                    "scoreline": score,
                    "is_correct_score": score == target,
                }
            )
    return rows


# --- CLOB -------------------------------------------------------------------


def _http_clob(
    url: str,
    proxy: str | None,
    timeout: float,
    *,
    method: str = "GET",
    body: str | None = None,
) -> str:
    """GET/POST CLOB JSON via urllib (SOCKS via pm.configure_proxy socket patch)."""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
    }
    data: bytes | None = None
    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"
        data = (body or "").encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    scheme = (urlparse(proxy).scheme or "").lower() if proxy else ""
    try:
        if proxy and scheme in ("http", "https"):
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
            with opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        if proxy and scheme.startswith("socks"):
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", errors="replace")[:300]
        raise QuoteError(f"CLOB HTTP {e.code}: {body_txt}") from e
    except urllib.error.URLError as e:
        raise QuoteError(f"CLOB network error: {e.reason}") from e


def fetch_books(
    token_ids: list[str],
    *,
    proxy: str | None | object = ...,
    timeout: float = 25.0,
    top_n: int = TOP_N,
    sequential_fallback: bool = False,
    max_sequential: int = 2,
) -> dict[str, dict[str, Any]]:
    """Return map token_id -> normalized book summary.

    On POST /books failure, default is fail-closed (mark missing) to avoid
    ``N × timeout`` hot-path stalls. Opt into limited sequential GET via
    ``sequential_fallback=True``.
    """
    ids = [str(t) for t in token_ids if t]
    if not ids:
        return {}
    if proxy is ...:
        proxy_url = pm.configure_proxy(None)
    else:
        proxy_url = pm.configure_proxy(None if proxy is None else str(proxy))

    books: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        payload = json.dumps([{"token_id": t} for t in chunk])
        url = f"{CLOB_BASE}/books"
        try:
            raw = _http_clob(url, proxy_url, timeout, method="POST", body=payload)
            data = json.loads(raw)
            if not isinstance(data, list):
                raise QuoteError("POST /books did not return a list")
            for book in data:
                if isinstance(book, dict):
                    tid = str(book.get("asset_id") or "")
                    if tid:
                        books[tid] = normalize_book(book, top_n=top_n)
        except (QuoteError, json.JSONDecodeError, pm.FetchError):
            if sequential_fallback:
                tried = 0
                for tid in chunk:
                    if tid in books:
                        continue
                    if tried >= max(0, int(max_sequential)):
                        break
                    tried += 1
                    try:
                        one = _http_clob(
                            f"{CLOB_BASE}/book?token_id={urllib.parse.quote(tid)}",
                            proxy_url,
                            min(float(timeout), 5.0),
                        )
                        book = json.loads(one)
                        if isinstance(book, dict):
                            books[tid] = normalize_book(book, top_n=top_n)
                    except Exception:  # noqa: BLE001
                        books[tid] = {
                            "token_id": tid,
                            "book_missing": True,
                            "best_bid": None,
                            "best_ask": None,
                            "error": "book_fetch_failed",
                        }
            for tid in chunk:
                if tid not in books:
                    books[tid] = {
                        "token_id": tid,
                        "book_missing": True,
                        "best_bid": None,
                        "best_ask": None,
                        "error": "books_batch_failed",
                    }
    for tid in ids:
        if tid not in books:
            books[tid] = {
                "token_id": tid,
                "book_missing": True,
                "best_bid": None,
                "best_ask": None,
            }
    return books


def normalize_book(book: dict[str, Any], *, top_n: int = TOP_N) -> dict[str, Any]:
    bids = list(book.get("bids") or [])
    asks = list(book.get("asks") or [])

    def _px(level: dict[str, Any]) -> float:
        try:
            return float(level.get("price"))
        except (TypeError, ValueError):
            return 0.0

    bids_sorted = sorted((b for b in bids if isinstance(b, dict)), key=_px, reverse=True)
    asks_sorted = sorted((a for a in asks if isinstance(a, dict)), key=_px)
    best_bid = best_ask = None
    best_bid_size = best_ask_size = None
    if bids_sorted:
        best_bid = _px(bids_sorted[0])
        try:
            best_bid_size = float(bids_sorted[0].get("size"))
        except (TypeError, ValueError):
            best_bid_size = None
    if asks_sorted:
        best_ask = _px(asks_sorted[0])
        try:
            best_ask_size = float(asks_sorted[0].get("size"))
        except (TypeError, ValueError):
            best_ask_size = None
    spread = None
    midpoint = None
    if best_bid is not None and best_ask is not None:
        spread = round(best_ask - best_bid, 6)
        midpoint = round((best_ask + best_bid) / 2, 6)
    ltp = book.get("last_trade_price")
    try:
        ltp_f = float(ltp) if ltp is not None and ltp != "" else None
    except (TypeError, ValueError):
        ltp_f = None
    return {
        "token_id": str(book.get("asset_id") or ""),
        "market": book.get("market") or "",
        "book_ts": str(book.get("timestamp") or ""),
        "hash": book.get("hash") or "",
        "best_bid": best_bid,
        "best_bid_size": best_bid_size,
        "best_ask": best_ask,
        "best_ask_size": best_ask_size,
        "spread": spread,
        "midpoint": midpoint,
        "last_trade_price": ltp_f,
        "bids_top": [
            {"price": str(b.get("price")), "size": str(b.get("size"))} for b in bids_sorted[:top_n]
        ],
        "asks_top": [
            {"price": str(a.get("price")), "size": str(a.get("size"))} for a in asks_sorted[:top_n]
        ],
        "tick_size": str(book.get("tick_size") or ""),
        "min_order_size": str(book.get("min_order_size") or ""),
        "neg_risk": bool(book.get("neg_risk")),
        "book_missing": False,
    }


def flag_misprice(
    settlement: str,
    book: dict[str, Any],
    *,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
) -> tuple[bool, str, dict[str, Any]]:
    """Return (is_opp, reason, economics).

    Only flag opportunities whose net edge after sports taker fee covers
    `min_net` (default 0.02 USDC/share). Economics always filled when priced.
    """
    meta: dict[str, Any] = {
        "fee_rate": fee_rate,
        "min_net": min_net,
        "gross_edge": None,
        "fee": None,
        "net_edge": None,
        "trade": None,
        "price": None,
    }
    if book.get("book_missing"):
        return False, "book_missing", meta
    bid = book.get("best_bid")
    ask = book.get("best_ask")

    if settlement == "WIN" and ask is not None:
        p = float(ask)
        if p >= 1.0 - 1e-12:
            return False, "ask_at_par", meta
        gross = 1.0 - p
        fee = taker_fee_per_share(p, fee_rate)
        net = gross - fee
        meta.update(
            {
                "trade": "buy_win",
                "price": p,
                "gross_edge": round(gross, 6),
                "fee": round(fee, 6),
                "net_edge": round(net, 6),
            }
        )
        if net + 1e-12 < float(min_net):
            return False, f"WIN ask={p} net={net:.4f} < min_net={min_net} (fee={fee:.4f})", meta
        if gross < float(eps):
            return False, f"WIN ask={p} gross<{eps}", meta
        return True, f"WIN buy ask={p} net={net:.4f} after fee={fee:.4f}", meta

    if settlement == "LOSE" and bid is not None:
        # sell_lose disabled: only buy_win on settled WIN legs.
        p = float(bid)
        meta.update(
            {
                "trade": "sell_lose",
                "price": p,
                "gross_edge": None,
                "fee": None,
                "net_edge": None,
            }
        )
        return False, "sell_lose_disabled", meta

    return False, "", meta


# --- Bridge join + pipeline -------------------------------------------------


def event_key(ev: dict[str, Any]) -> str:
    """Semantic identity for score/FT/player (no wall-clock ts — avoids duplicate buys)."""
    typ = ev.get("type") or ""
    mid = ev.get("match_id") or ""
    if typ == "score_change":
        prev = ev.get("prev") or {}
        curr = ev.get("curr") or {}
        return (
            f"score_change|{mid}|"
            f"{prev.get('home')}-{prev.get('away')}->"
            f"{curr.get('home')}-{curr.get('away')}"
        )
    if typ == "player_stat_change":
        pid = (
            ev.get("person_id")
            or ev.get("player_id")
            or ev.get("person_en_name")
            or ev.get("person_name")
            or ev.get("player")
            or ev.get("player_name")
            or ""
        )
        changes = ev.get("changes")
        if isinstance(changes, dict) and changes:
            ch = ",".join(f"{k}:{changes[k]}" for k in sorted(changes))
        else:
            ch = str(
                ev.get("stat")
                or ev.get("category")
                or ev.get("stat_type")
                or ""
            )
        curr = ev.get("curr", ev.get("value", ev.get("current", "")))
        if isinstance(curr, dict):
            curr_s = ",".join(f"{k}:{curr.get(k)}" for k in sorted(curr))
        else:
            curr_s = str(curr)
        return f"player_stat_change|{mid}|{pid}|{ch}|{curr_s}"
    curr = ev.get("curr") if isinstance(ev.get("curr"), dict) else {}
    hs = ev.get("home_score", curr.get("home") if isinstance(curr, dict) else None)
    aws = ev.get("away_score", curr.get("away") if isinstance(curr, dict) else None)
    return f"{typ}|{mid}|{hs}-{aws}"


def _event_side_score(ev: dict[str, Any], side: str) -> Any:
    """Prefer top-level home_score/away_score; fall back to curr.{side}."""
    key = f"{side}_score"
    if ev.get(key) is not None and ev.get(key) != "":
        return ev.get(key)
    curr = ev.get("curr")
    if isinstance(curr, dict) and curr.get(side) is not None and curr.get(side) != "":
        return curr.get(side)
    return None


def load_bridge_matches(root: Path) -> list[dict[str, Any]]:
    snap = load_json(bridge_dir(root) / "matches.json", {}) or {}
    return list(snap.get("matches") or [])


def load_bridge_quote_events(
    root: Path,
    *,
    byte_offset: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Load score_change + match_finished events for quoting.

    When ``byte_offset`` is set, only read new bytes from that position (fast path).
    Returns ``(events, new_offset)``.

    If the file shrank (data_prune rewrite) so ``byte_offset > size``, clamp to
    EOF and return no events — do **not** rewind to 0. A rewind would replay
    every retained score_change into AF referee and burn API quota.
    """
    path = bridge_dir(root) / "events.jsonl"
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows, 0
    try:
        size = path.stat().st_size
    except OSError:
        return rows, 0
    start = int(byte_offset or 0)
    if start < 0:
        start = 0
    if start > size:
        # Prune/truncate removed a prefix we already consumed — stay caught up.
        return rows, size
    try:
        with path.open("rb") as f:
            if start:
                f.seek(start)
            raw = f.read().decode("utf-8", errors="replace")
            new_off = f.tell()
    except OSError:
        return rows, start
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") in (
            "match_finished", "score_change", "player_stat_change"
        ):
            rows.append(ev)
    return rows, int(new_off)


def load_bridge_ft_events(root: Path) -> list[dict[str, Any]]:
    """Backward-compatible alias."""
    rows, _ = load_bridge_quote_events(root, byte_offset=0)
    return [e for e in rows if e.get("type") == "match_finished"]


def join_ft_context(root: Path, ev: dict[str, Any]) -> dict[str, Any]:
    """Merge FT/score event with full matched row from matches.json."""
    mid = str(ev.get("match_id") or "")
    eid = str((ev.get("polymarket") or {}).get("event_id") or "")
    row = None
    for r in load_bridge_matches(root):
        dqd = r.get("dongqiudi") or {}
        pm = r.get("polymarket") or {}
        if mid and str(dqd.get("id") or "") == mid:
            row = r
            break
        if eid and str(pm.get("event_id") or "") == eid:
            row = r
            break
    pm = dict((row or {}).get("polymarket") or {})
    # Overlay slim event polymarket
    pm.update({k: v for k, v in (ev.get("polymarket") or {}).items() if v})
    dqd = dict((row or {}).get("dongqiudi") or {})
    hs = _event_side_score(ev, "home")
    aws = _event_side_score(ev, "away")
    if hs is not None:
        dqd["home_score"] = hs
    if aws is not None:
        dqd["away_score"] = aws
    return {
        "event": ev,
        "match_row": row,
        "dongqiudi": dqd,
        "polymarket": pm,
        "home_score": hs if hs is not None else dqd.get("home_score"),
        "away_score": aws if aws is not None else dqd.get("away_score"),
        "home": ev.get("home") or pm.get("home") or dqd.get("home") or "",
        "away": ev.get("away") or pm.get("away") or dqd.get("away") or "",
    }


def markets_from_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize bridge market_refs (may lack outcomes) into enrich_market shape."""
    out: list[dict[str, Any]] = []
    for r in refs or []:
        tokens = [str(t) for t in (r.get("clob_token_ids") or []) if t]
        out.append(
            {
                "market_id": str(r.get("market_id") or ""),
                "condition_id": str(r.get("condition_id") or ""),
                "question": str(r.get("question") or ""),
                "group_item_title": "",
                "slug": str(r.get("slug") or ""),
                "sports_market_type": str(r.get("sports_market_type") or ""),
                "outcomes": list(r.get("outcomes") or ["Yes", "No"]),
                "clob_token_ids": tokens,
                "neg_risk": bool(r.get("neg_risk", True)),
            }
        )
    return out


def collect_target_tokens(
    ctx: dict[str, Any],
    *,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
    mode: str = "ft",
    market_cache: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build settled token rows + discovery meta.

    mode=ft: full moneyline + props + exact (final settlement).
    mode=live: only outcomes already locked by current score (no moneyline/spreads).

    When ``market_cache`` is set, prefer warmed Gamma catalog (main / more / exact)
    so the hot path skips Gamma HTTP; misses fetch once and write back.
    """
    pm_h = ctx["polymarket"]
    home, away = ctx["home"], ctx["away"]
    hs, aws = ctx["home_score"], ctx["away_score"]
    match_id = str(
        (ctx.get("event") or {}).get("match_id")
        or (ctx.get("dongqiudi") or {}).get("id")
        or ""
    )
    meta: dict[str, Any] = {
        "mode": mode,
        "main_event": None,
        "more_markets": None,
        "exact_score": None,
        "skipped": [],
        "catalog_cache": "none",
    }

    cached: dict[str, Any] | None = None
    if market_cache is not None and match_id:
        try:
            hit = market_cache.get(match_id)
            if isinstance(hit, dict):
                cached = hit
        except Exception:  # noqa: BLE001
            cached = None

    main_from_cache = bool(cached and isinstance(cached.get("main_event"), dict))
    related_from_cache = bool(cached and cached.get("related_complete"))
    if main_from_cache and related_from_cache:
        meta["catalog_cache"] = "hit"
    elif cached and (main_from_cache or related_from_cache or cached.get("more_markets") or cached.get("exact_score")):
        meta["catalog_cache"] = "partial"

    main_markets: list[dict[str, Any]] = []
    ev: dict[str, Any] | None = None
    if main_from_cache:
        ev = cached["main_event"]  # type: ignore[index]
    else:
        try:
            ev = fetch_gamma_event(
                event_id=str(pm_h.get("event_id") or "") or None,
                slug=str(pm_h.get("slug") or "") or None,
                proxy=proxy,
            )
            if meta["catalog_cache"] not in ("hit", "partial"):
                meta["catalog_cache"] = "miss"
        except pm.FetchError as e:
            ev = None
            meta["main_fetch_error"] = str(e)
            if meta["catalog_cache"] not in ("hit", "partial"):
                meta["catalog_cache"] = "miss"
    if ev:
        meta["main_event"] = {
            "id": str(ev.get("id") or ""),
            "slug": ev.get("slug") or "",
            "title": ev.get("title") or "",
        }
        main_markets = [enrich_market(m) for m in (ev.get("markets") or []) if isinstance(m, dict)]
    if not main_markets:
        main_markets = markets_from_refs(list(pm_h.get("market_refs") or []))

    tokens: list[dict[str, Any]] = []
    if mode == "ft":
        tokens.extend(
            moneyline_tokens(main_markets, home=home, away=away, home_score=hs, away_score=aws)
        )
        ml_n = len([t for t in tokens if t["family"] == "moneyline"])
        # Two-way team-name markets yield 2 tokens; legacy Yes/No yields up to 4.
        if ml_n < 2:
            meta["skipped"].append({"family": "moneyline", "reason": "incomplete_moneyline_markets"})
    else:
        meta["skipped"].append({"family": "moneyline", "reason": "live_not_locked_until_ft"})
        # Basketball spreads are never mid-game locked (both teams keep scoring).
        meta["skipped"].append({"family": "spreads", "reason": "live_not_locked"})

    related: dict[str, Any] = {"more_markets": None, "exact_score": None}
    related_complete = False
    if related_from_cache:
        related = {
            "more_markets": cached.get("more_markets"),  # type: ignore[union-attr]
            "exact_score": cached.get("exact_score"),  # type: ignore[union-attr]
        }
        related_complete = True
    elif include_props or include_exact:
        slug = str(pm_h.get("slug") or "")
        if slug:
            try:
                related = discover_related_events(slug=slug, home=home, away=away, proxy=proxy)
                related_complete = True
                if meta["catalog_cache"] == "hit":
                    meta["catalog_cache"] = "partial"
                elif meta["catalog_cache"] not in ("miss", "partial"):
                    meta["catalog_cache"] = "miss"
            except pm.FetchError as e:
                meta["related_fetch_error"] = str(e)
                # Keep any partial siblings from an incomplete warm; do not mark complete.
                if cached:
                    related = {
                        "more_markets": cached.get("more_markets"),
                        "exact_score": cached.get("exact_score"),
                    }
                related_complete = False
        else:
            related_complete = True

    # Persist when we newly completed related discovery and/or filled main.
    if market_cache is not None and match_id and (ev or related_complete):
        already = bool(
            cached
            and cached.get("related_complete")
            and isinstance(cached.get("main_event"), dict)
        )
        improved = (related_complete and not (cached and cached.get("related_complete"))) or (
            isinstance(ev, dict) and not (cached and isinstance(cached.get("main_event"), dict))
        )
        if improved or (related_complete and isinstance(ev, dict) and not already):
            try:
                market_cache.put(
                    match_id,
                    {
                        "event_id": str(pm_h.get("event_id") or ""),
                        "slug": str(pm_h.get("slug") or ""),
                        "home": home,
                        "away": away,
                        "main_event": ev
                        if isinstance(ev, dict)
                        else (cached or {}).get("main_event"),
                        "more_markets": related.get("more_markets"),
                        "exact_score": related.get("exact_score"),
                        "related_complete": bool(
                            related_complete
                            and isinstance(
                                ev if isinstance(ev, dict) else (cached or {}).get("main_event"),
                                dict,
                            )
                        ),
                    },
                )
            except Exception:  # noqa: BLE001
                pass

    if include_props:
        more = related.get("more_markets")
        prop_markets: list[dict[str, Any]] = []
        if more:
            meta["more_markets"] = {
                "id": str(more.get("id") or ""),
                "slug": more.get("slug") or "",
                "title": more.get("title") or "",
            }
            prop_markets.extend(
                enrich_market(m) for m in (more.get("markets") or []) if isinstance(m, dict)
            )
        # Basketball Gamma often puts totals/spreads/props on the main event.
        prop_markets.extend(main_markets)
        prop_markets.extend(markets_from_refs(list(pm_h.get("market_refs") or [])))
        # De-dupe by market_id / condition_id / question.
        seen_m: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for mkt in prop_markets:
            mk = str(
                mkt.get("market_id")
                or mkt.get("condition_id")
                or mkt.get("question")
                or id(mkt)
            )
            if mk in seen_m:
                continue
            seen_m.add(mk)
            deduped.append(mkt)
        prop_markets = deduped
        if prop_markets:
            dqd = ctx.get("dongqiudi") or {}
            if mode == "ft":
                tokens.extend(
                    spread_tokens(
                        prop_markets, home=home, away=away, home_score=hs, away_score=aws
                    )
                )
            tokens.extend(
                totals_tokens(
                    prop_markets,
                    home=home,
                    away=away,
                    home_score=hs,
                    away_score=aws,
                    home_half=dqd.get("home_half"),
                    away_half=dqd.get("away_half"),
                    mode=mode,
                )
            )
        else:
            meta["skipped"].append({"family": "props", "reason": "not_listed"})

    if include_exact:
        ex = related.get("exact_score")
        if ex:
            meta["exact_score"] = {
                "id": str(ex.get("id") or ""),
                "slug": ex.get("slug") or "",
                "title": ex.get("title") or "",
            }
            exact_markets = [
                enrich_market(m) for m in (ex.get("markets") or []) if isinstance(m, dict)
            ]
            tokens.extend(
                exact_score_tokens(
                    exact_markets,
                    home=home,
                    away=away,
                    home_score=hs,
                    away_score=aws,
                    mode=mode,
                )
            )
        else:
            meta["skipped"].append({"family": "exact_score", "reason": "not_listed"})

    return tokens, meta


def tradeable_token_rows(token_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop settled LOSE legs before CLOB /books (sell_lose disabled)."""
    return [
        r
        for r in token_rows
        if str(r.get("settlement") or "").upper() != "LOSE"
    ]


def quote_tokens(
    token_rows: list[dict[str, Any]],
    *,
    proxy: str | None | object = ...,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
    trade_executor: Any | None = None,
    event_key_str: str = "",
    match_meta: dict[str, Any] | None = None,
    books: dict[str, dict[str, Any]] | None = None,
    trade_workers: int | None = None,
) -> list[dict[str, Any]]:
    token_rows = tradeable_token_rows(token_rows)
    ids = [r["token_id"] for r in token_rows if r.get("token_id")]
    if books is not None:
        book_map = books
    elif ids:
        book_map = fetch_books(ids, proxy=proxy)
    else:
        book_map = {}
    t0 = time.monotonic()
    priced: list[dict[str, Any]] = []
    trade_jobs: list[tuple[int, dict[str, Any]]] = []
    for row in token_rows:
        tid = row["token_id"]
        book = book_map.get(tid) or {"book_missing": True, "best_bid": None, "best_ask": None}
        mis, reason, econ = flag_misprice(
            str(row.get("settlement") or ""),
            book,
            eps=eps,
            fee_rate=fee_rate,
            min_net=min_net,
        )
        item = {
            **row,
            "best_bid": book.get("best_bid"),
            "best_bid_size": book.get("best_bid_size"),
            "best_ask": book.get("best_ask"),
            "best_ask_size": book.get("best_ask_size"),
            "spread": book.get("spread"),
            "midpoint": book.get("midpoint"),
            "last_trade_price": book.get("last_trade_price"),
            "bids_top": book.get("bids_top") or [],
            "asks_top": book.get("asks_top") or [],
            "tick_size": book.get("tick_size") or "",
            "neg_risk": book.get("neg_risk"),
            "book_ts": book.get("book_ts") or "",
            "book_missing": bool(book.get("book_missing")),
            "misprice": mis,
            "misprice_reason": reason,
            "gross_edge": econ.get("gross_edge"),
            "fee": econ.get("fee"),
            "net_edge": econ.get("net_edge"),
            "trade": econ.get("trade"),
        }
        if mis and trade_executor is not None:
            trade_jobs.append((len(priced), item))
        priced.append(item)

    def _one_trade(item: dict[str, Any]) -> dict[str, Any]:
        try:
            trade_row = trade_executor.maybe_trade(
                item,
                event_key=event_key_str,
                match_meta=match_meta,
                event_type=str((match_meta or {}).get("event_type") or ""),
            )
            if trade_row is not None:
                return {
                    "status": trade_row.get("status"),
                    "success": trade_row.get("success"),
                    "skip_reason": trade_row.get("skip_reason"),
                    "plan": trade_row.get("plan"),
                    "live": trade_row.get("live"),
                }
            return {"status": "skipped", "skip_reason": "no_row"}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "skip_reason": str(e)}

    workers = TRADE_POOL_WORKERS if trade_workers is None else max(1, int(trade_workers))
    if trade_jobs:
        if workers <= 1 or len(trade_jobs) == 1:
            for idx, item in trade_jobs:
                priced[idx]["trade_attempt"] = _one_trade(item)
        else:
            pool = _trade_executor_pool()
            futs = {
                pool.submit(_one_trade, item): idx for idx, item in trade_jobs
            }
            for fut in as_completed(futs):
                priced[futs[fut]]["trade_attempt"] = fut.result()

    books_ms = int((time.monotonic() - t0) * 1000) if books is not None else None
    if books_ms is not None:
        for item in priced:
            item.setdefault("latency_ms", {})
            # books RTT attributed outside when prefetched
    return priced


def build_bundle(
    ctx: dict[str, Any],
    quotes: list[dict[str, Any]],
    discovery: dict[str, Any],
) -> dict[str, Any]:
    ev = ctx.get("event") or {}
    mode = (discovery or {}).get("mode") or (
        "live" if ev.get("type") == "score_change" else "ft"
    )
    winner = winner_from_score(ctx["home_score"], ctx["away_score"]) if mode == "ft" else None
    opps = [q for q in quotes if q.get("misprice")]
    return {
        "quoted_at": now_cn_iso(),
        "source": "polymarket-quote",
        "trigger": ev.get("type") or "manual",
        "mode": mode,
        "match_id": ev.get("match_id") or (ctx.get("dongqiudi") or {}).get("id") or "",
        "event_key": event_key(ev),
        "home": ctx["home"],
        "away": ctx["away"],
        "home_score": ctx["home_score"],
        "away_score": ctx["away_score"],
        "prev_score": ev.get("prev"),
        "winner": winner,
        "polymarket": {
            "event_id": (ctx.get("polymarket") or {}).get("event_id") or "",
            "slug": (ctx.get("polymarket") or {}).get("slug") or "",
            "url": (ctx.get("polymarket") or {}).get("url") or "",
        },
        "discovery": discovery,
        "count": len(quotes),
        "opportunity_count": len(opps),
        "quotes": quotes,
        "opportunities": [
            {
                "market_key": q.get("market_key"),
                "family": q.get("family"),
                "outcome": q.get("outcome"),
                "settlement": q.get("settlement"),
                "token_id": q.get("token_id"),
                "trade": q.get("trade"),
                "best_bid": q.get("best_bid"),
                "best_ask": q.get("best_ask"),
                "gross_edge": q.get("gross_edge"),
                "fee": q.get("fee"),
                "net_edge": q.get("net_edge"),
                "misprice_reason": q.get("misprice_reason"),
                "question": q.get("question"),
            }
            for q in opps
        ],
    }


def persist_bundle(
    root: Path,
    bundle: dict[str, Any],
    *,
    write_opportunities: bool = True,
) -> None:
    ddir = data_dir(root)
    write_json(ddir / "latest.json", bundle)
    append_jsonl(ddir / "quotes.jsonl", [bundle])
    if write_opportunities and bundle.get("opportunities"):
        append_jsonl(
            ddir / "opportunities.jsonl",
            [
                {
                    "quoted_at": bundle.get("quoted_at"),
                    "match_id": bundle.get("match_id"),
                    "home": bundle.get("home"),
                    "away": bundle.get("away"),
                    "home_score": bundle.get("home_score"),
                    "away_score": bundle.get("away_score"),
                    **opp,
                }
                for opp in bundle["opportunities"]
            ],
        )


def load_cursor(root: Path) -> dict[str, Any]:
    return load_json(data_dir(root) / "cursor.json", {"processed_keys": []}) or {
        "processed_keys": []
    }


def save_cursor(root: Path, cursor: dict[str, Any]) -> None:
    write_json(data_dir(root) / "cursor.json", cursor)


def quote_bridge_event(
    root: Path,
    ev: dict[str, Any],
    *,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
    persist: bool = True,
    trade_executor: Any | None = None,
    market_cache: Any | None = None,
) -> dict[str, Any]:
    ctx = join_ft_context(root, ev)
    if ctx["home_score"] is None or ctx["away_score"] is None:
        raise QuoteError("missing home_score/away_score on event")
    if not (ctx.get("polymarket") or {}).get("event_id") and not (ctx.get("polymarket") or {}).get(
        "slug"
    ):
        raise QuoteError("missing polymarket event_id/slug after join")
    mode = "live" if ev.get("type") in ("score_change", "player_stat_change") else "ft"
    tokens, discovery = collect_target_tokens(
        ctx,
        proxy=proxy,
        include_props=include_props,
        include_exact=include_exact,
        mode=mode,
        market_cache=market_cache,
    )
    prop_refs = list((ctx.get("polymarket") or {}).get("props") or [])
    prop_markets = markets_from_refs(prop_refs)
    # Also accept player props that live in market_refs.
    for ref in list((ctx.get("polymarket") or {}).get("market_refs") or []):
        cat = str(ref.get("category") or ref.get("sports_market_type") or "").lower()
        if "player" in cat or cat in (
            "points",
            "assists",
            "rebounds",
            "steals",
            "blocks",
            "threes",
        ):
            prop_markets.extend(markets_from_refs([ref]))

    if ev.get("type") == "player_stat_change":
        player = str(
            ev.get("person_en_name")
            or ev.get("person_name")
            or ev.get("player")
            or ev.get("player_name")
            or ev.get("name")
            or ""
        )
        curr = ev.get("curr", ev.get("value", ev.get("current")))
        if isinstance(curr, dict):
            changed = ev.get("changes") or curr
            tokens = [
                row
                for changed_stat in changed
                for row in player_prop_tokens(
                    prop_markets,
                    player=player,
                    stat=str(changed_stat),
                    curr=curr.get(changed_stat),
                    mode="live",
                )
            ]
        else:
            stat = str(ev.get("stat") or ev.get("category") or ev.get("stat_type") or "")
            tokens = player_prop_tokens(
                prop_markets, player=player, stat=stat, curr=curr, mode="live"
            )
        discovery["player_stat"] = {"player": player, "curr": curr}
    elif ev.get("type") == "match_finished" and include_props and prop_markets:
        # Settle all remaining player props from final boxscore (Over + Under).
        box = ev.get("box") if isinstance(ev.get("box"), dict) else None
        if not box:
            box = _load_final_box(root, str(ev.get("match_id") or ""))
        prop_rows: list[dict[str, Any]] = []
        if isinstance(box, dict):
            for _pid, player in box.items():
                if not isinstance(player, dict):
                    continue
                pname = str(
                    player.get("person_en_name")
                    or player.get("person_name")
                    or player.get("name")
                    or ""
                )
                for stat_key in ("score", "assist", "rebound", "steal", "block", "three_point"):
                    if player.get(stat_key) is None:
                        continue
                    prop_rows.extend(
                        player_prop_tokens(
                            prop_markets,
                            player=pname,
                            stat=stat_key,
                            curr=player.get(stat_key),
                            mode="ft",
                        )
                    )
        if prop_rows:
            tokens.extend(prop_rows)
            discovery["ft_player_props"] = len(prop_rows)
    n_before = len(tokens)
    tokens = tradeable_token_rows(tokens)
    discovery["skipped_lose_tokens"] = max(0, n_before - len(tokens))
    ek = event_key(ev)
    match_meta = {
        "match_id": ev.get("match_id") or (ctx.get("dongqiudi") or {}).get("id") or "",
        "home": ctx.get("home") or "",
        "away": ctx.get("away") or "",
        "home_score": ctx.get("home_score"),
        "away_score": ctx.get("away_score"),
        "event_type": str(ev.get("type") or ""),
    }
    quote_kw = dict(
        proxy=proxy,
        eps=eps,
        fee_rate=fee_rate,
        min_net=min_net,
        trade_executor=trade_executor,
        event_key_str=ek,
        match_meta=match_meta,
    )
    latency_ms: dict[str, int] = {}
    # Live: one books POST for all tokens, trade totals/props first then rest.
    if mode == "live":
        phase_a = [t for t in tokens if t.get("family") in ("totals", "player_prop")]
        phase_a_ids = {id(t) for t in phase_a}
        phase_b = [t for t in tokens if id(t) not in phase_a_ids]
        all_ids = [t["token_id"] for t in tokens if t.get("token_id")]
        t_books = time.monotonic()
        book_map = fetch_books(all_ids, proxy=proxy) if all_ids else {}
        latency_ms["books"] = int((time.monotonic() - t_books) * 1000)
        quotes: list[dict[str, Any]] = []
        t_trade = time.monotonic()
        if phase_a:
            quotes.extend(quote_tokens(phase_a, books=book_map, **quote_kw))
        if phase_b:
            quotes.extend(quote_tokens(phase_b, books=book_map, **quote_kw))
        latency_ms["trade_phases"] = int((time.monotonic() - t_trade) * 1000)
        discovery["quote_phases"] = ["totals_props", "rest"]
        discovery["books_once"] = True
    else:
        t_books = time.monotonic()
        quotes = quote_tokens(tokens, **quote_kw)
        latency_ms["books_and_trade"] = int((time.monotonic() - t_books) * 1000)
    discovery["latency_ms"] = latency_ms
    bundle = build_bundle(ctx, quotes, discovery)
    bundle["trigger"] = str(ev.get("type") or "")
    bundle["event_key"] = ek
    bundle["mode"] = mode
    if persist:
        persist_bundle(root, bundle)
    return bundle


def _load_final_box(root: Path, match_id: str) -> dict[str, Any]:
    """Load last DQD boxscore players keyed by person_id, if present."""
    if not match_id:
        return {}
    path = root / "data" / "boxscores" / f"{match_id}.json"
    raw = load_json(path, None)
    if not isinstance(raw, dict):
        return {}
    try:
        dqd_scripts = Path(__file__).resolve().parents[2] / "dongqiudi-basketball" / "scripts"
        if str(dqd_scripts) not in sys.path:
            sys.path.insert(0, str(dqd_scripts))
        import dqd_lib as dqd  # type: ignore

        players = dqd.extract_player_stats(raw)
        dqd.apply_english_person_names(players)
        return players if isinstance(players, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def quote_finished_event(
    root: Path,
    ev: dict[str, Any],
    *,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
    persist: bool = True,
    trade_executor: Any | None = None,
    market_cache: Any | None = None,
) -> dict[str, Any]:
    """Alias for FT / manual quoting."""
    if not ev.get("type"):
        ev = dict(ev)
        ev["type"] = "match_finished"
    return quote_bridge_event(
        root,
        ev,
        proxy=proxy,
        include_props=include_props,
        include_exact=include_exact,
        eps=eps,
        fee_rate=fee_rate,
        min_net=min_net,
        persist=persist,
        trade_executor=trade_executor,
        market_cache=market_cache,
    )


def process_bridge_events(
    root: Path,
    *,
    proxy: str | None | object = ...,
    include_props: bool = True,
    include_exact: bool = True,
    eps: float = DEFAULT_EPS,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
    min_net: float = DEFAULT_MIN_NET,
    force: bool = False,
    trade_executor: Any | None = None,
    market_cache: Any | None = None,
    af_referee: Any | None = None,
    af_mode: str = "gate",
    events_override: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    # Basketball path: DQD events are authoritative.  The legacy AF implementation
    # remains below only as inert compatibility code for old saved sessions.
    return _process_basketball_events(
        root,
        proxy=proxy,
        include_props=include_props,
        include_exact=include_exact,
        eps=eps,
        fee_rate=fee_rate,
        min_net=min_net,
        force=force,
        trade_executor=trade_executor,
        market_cache=market_cache,
        events_override=events_override,
    )


_MATCH_TRADE_LOCKS_GUARD = threading.Lock()
_MATCH_TRADE_LOCKS: dict[str, threading.RLock] = {}


def _match_trade_lock(match_id: str) -> threading.RLock:
    with _MATCH_TRADE_LOCKS_GUARD:
        return _MATCH_TRADE_LOCKS.setdefault(match_id or "_unknown", threading.RLock())


def _process_basketball_events(
    root: Path,
    *,
    proxy: str | None | object,
    include_props: bool,
    include_exact: bool,
    eps: float,
    fee_rate: float,
    min_net: float,
    force: bool,
    trade_executor: Any | None,
    market_cache: Any | None,
    events_override: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Process trusted DQD basketball events, serialized per match."""
    cursor = load_cursor(root)
    seen = set(cursor.get("processed_keys") or [])
    bundles: list[dict[str, Any]] = []
    if trade_executor is not None:
        try:
            bundles.extend(trade_executor.retry_pending_flattens() or [])
        except Exception as exc:  # noqa: BLE001
            bundles.append({"quoted_at": now_cn_iso(), "trigger": "flatten_retry", "error": str(exc)})

    events: list[dict[str, Any]] = list(events_override or [])
    override_keys = {event_key(ev) for ev in events if isinstance(ev, dict)}
    offset = int(cursor.get("events_byte_offset") or 0)
    file_events, new_offset = load_bridge_quote_events(root, byte_offset=offset)
    events.extend(ev for ev in file_events if event_key(ev) not in override_keys)
    file_keys = {event_key(ev) for ev in file_events if isinstance(ev, dict)}
    # Only advance the durable byte cursor when every newly-read file event
    # either succeeds or is already in ``seen``. A failure keeps the old offset
    # so the event can be retried; successes remain protected by ``seen``.
    file_progress_ok = True

    for ev in events:
        if not isinstance(ev, dict):
            continue
        typ = str(ev.get("type") or "")
        if typ not in ("score_change", "player_stat_change", "match_finished"):
            continue
        key = event_key(ev)
        if key in seen and not force:
            continue
        mid = str(ev.get("match_id") or "")
        with _match_trade_lock(mid):
            curr = ev.get("curr") if isinstance(ev.get("curr"), dict) else {}
            # Normalize scores onto the event for flatten / logging.
            if ev.get("home_score") is None and curr.get("home") is not None:
                ev["home_score"] = curr.get("home")
            if ev.get("away_score") is None and curr.get("away") is not None:
                ev["away_score"] = curr.get("away")
            if typ == "score_change" and (
                bool(ev.get("is_reversal"))
                or any(
                    int((ev.get("curr") or {}).get(side, 0) or 0)
                    < int((ev.get("prev") or {}).get(side, 0) or 0)
                    for side in ("home", "away")
                )
            ):
                flattened = []
                if trade_executor is not None:
                    flattened = list(trade_executor.maybe_flatten_for_event(ev) or [])
                bundles.append(
                    {
                        "quoted_at": now_cn_iso(),
                        "trigger": typ,
                        "mode": "reversal_flatten",
                        "match_id": mid,
                        "event_key": key,
                        "flatten_attempts": flattened,
                        "flatten_count": len(flattened),
                    }
                )
                seen.add(key)
                continue
            try:
                bundle = quote_bridge_event(
                    root,
                    ev,
                    proxy=proxy,
                    include_props=include_props,
                    include_exact=False,
                    eps=eps,
                    fee_rate=fee_rate,
                    min_net=min_net,
                    persist=True,
                    trade_executor=trade_executor,
                    market_cache=market_cache,
                )
                bundles.append(bundle)
                if typ == "match_finished" and market_cache is not None and mid:
                    market_cache.drop(mid)
                seen.add(key)
            except Exception as exc:  # noqa: BLE001
                bundles.append(
                    {
                        "quoted_at": now_cn_iso(),
                        "trigger": typ,
                        "match_id": mid,
                        "event_key": key,
                        "error": str(exc),
                    }
                )
                if key in file_keys:
                    file_progress_ok = False

    if file_progress_ok:
        cursor["events_byte_offset"] = new_offset
    cursor["processed_keys"] = sorted(seen)[-1000:]
    cursor["updated_at"] = now_cn_iso()
    save_cursor(root, cursor)
    return bundles


def find_match_row(
    root: Path,
    *,
    match_id: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any] | None:
    for r in load_bridge_matches(root):
        dqd = r.get("dongqiudi") or {}
        pm = r.get("polymarket") or {}
        if match_id and str(dqd.get("id") or "") == str(match_id):
            return r
        if event_id and str(pm.get("event_id") or "") == str(event_id):
            return r
    return None


def synthetic_ft_from_row(
    row: dict[str, Any],
    *,
    home_score: int,
    away_score: int,
) -> dict[str, Any]:
    dqd = row.get("dongqiudi") or {}
    pm = row.get("polymarket") or {}
    return {
        "type": "match_finished",
        "ts": now_cn_iso(),
        "match_id": str(dqd.get("id") or ""),
        "prev_status": "playing",
        "status": "played",
        "status_display": "Played",
        "league": pm.get("league") or dqd.get("league") or "",
        "home": pm.get("home") or dqd.get("home") or "",
        "away": pm.get("away") or dqd.get("away") or "",
        "home_score": home_score,
        "away_score": away_score,
        "kickoff_beijing": row.get("kickoff_beijing") or "",
        "official_clock": "FT",
        "polymarket": {
            "event_id": pm.get("event_id") or "",
            "slug": pm.get("slug") or "",
            "url": pm.get("url") or "",
            "condition_ids": list(pm.get("condition_ids") or []),
            "market_refs": list(pm.get("market_refs") or []),
        },
    }


BRIDGE_BOARD_URL = "http://127.0.0.1:8789"
_OWNED_BRIDGE: Any = None


def get_owned_bridge() -> Any | None:
    return _OWNED_BRIDGE


def _http_json(method: str, url: str, *, timeout: float = 2.0) -> dict[str, Any] | None:
    import urllib.request

    req = urllib.request.Request(
        url,
        method=method,
        data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return None


def ensure_upstream_bridge(
    root: Path,
    *,
    prefer_board: bool | None = None,
) -> dict[str, Any]:
    """Start match-bridge (dongqiudi-match + polymarket-soccer).

    Default: **in-process** BridgeRuntime owned by quote (memory event queue).
    Set ``MAIN_BRIDGE_INPROC=0`` (or ``prefer_board=True``) to use bridge-board
    on :8789 instead (file-wake path).

    In-process mode refuses to co-exist with a running board skill (dual writers).
    If the board is running, it is stopped via ``POST /api/bridge/stop`` first.
    """
    global _OWNED_BRIDGE

    if prefer_board is None:
        prefer_board = not _env_bool("MAIN_BRIDGE_INPROC", True)

    if prefer_board:
        st = _http_json("GET", f"{BRIDGE_BOARD_URL}/api/status", timeout=1.5)
        if st is not None:
            if st.get("running"):
                return {
                    "ok": True,
                    "mode": "bridge_board",
                    "already": True,
                    "url": BRIDGE_BOARD_URL,
                    "dqd_ticks": st.get("dqd_ticks"),
                    "pm_ticks": st.get("pm_ticks"),
                }
            started = _http_json("POST", f"{BRIDGE_BOARD_URL}/api/bridge/start", timeout=120)
            if started and started.get("ok"):
                return {
                    "ok": True,
                    "mode": "bridge_board",
                    "already": bool(started.get("already")),
                    "url": BRIDGE_BOARD_URL,
                    "dqd_ticks": started.get("dqd_ticks"),
                    "pm_ticks": started.get("pm_ticks"),
                }

    # In-process owner: stop board skill if it is already emitting events.
    st = _http_json("GET", f"{BRIDGE_BOARD_URL}/api/status", timeout=1.5)
    if st is not None and st.get("running"):
        print(
            "upstream → bridge-board skill is running; stopping it so "
            "quote can own in-process match-bridge (avoid dual emitters)",
            flush=True,
        )
        stopped = _http_json("POST", f"{BRIDGE_BOARD_URL}/api/bridge/stop", timeout=30)
        if stopped is None or (
            stopped.get("running") is True and not stopped.get("ok", True)
        ):
            # Still running — refuse rather than double-write.
            if stopped and stopped.get("running"):
                raise RuntimeError(
                    "bridge-board skill still running; stop it before "
                    "MAIN_BRIDGE_INPROC=1 / in-process bridge"
                )
        # Re-check
        st2 = _http_json("GET", f"{BRIDGE_BOARD_URL}/api/status", timeout=1.5)
        if st2 is not None and st2.get("running"):
            raise RuntimeError(
                "bridge-board skill still running after stop; "
                "refuse in-process bridge to avoid dual score_change emitters"
            )

    bridge_scripts = root / ".cursor" / "skills" / "match-bridge" / "scripts"
    if str(bridge_scripts) not in sys.path:
        sys.path.insert(0, str(bridge_scripts))
    import bridge_lib as bridge  # type: ignore

    if _OWNED_BRIDGE is None:
        _OWNED_BRIDGE = bridge.BridgeRuntime(root, async_persist=True)
    result = _OWNED_BRIDGE.start()
    # Mutual exclusion marker so bridge-board Start refuses dual emit.
    try:
        marker = root / "data" / "bridge" / ".inproc_owner"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            f"pid={os.getpid()}\nmode=in_process\n", encoding="utf-8"
        )
    except OSError:
        pass
    return {
        "ok": True,
        "mode": "in_process",
        "already": bool(result.get("already")),
        "dqd_interval": _OWNED_BRIDGE.dqd_interval,
        "dqd_idle_interval": _OWNED_BRIDGE.dqd_idle_interval,
        "pm_interval": _OWNED_BRIDGE.pm_interval,
        "owned": True,
        "event_queue": True,
    }


def stop_owned_bridge() -> None:
    """Stop in-process bridge started by ensure_upstream_bridge (if any)."""
    global _OWNED_BRIDGE
    if _OWNED_BRIDGE is None:
        return
    owned = _OWNED_BRIDGE
    root = Path(owned.root)
    try:
        owned.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        marker = root / "data" / "bridge" / ".inproc_owner"
        if marker.is_file():
            marker.unlink()
    except Exception:  # noqa: BLE001
        pass
    _OWNED_BRIDGE = None

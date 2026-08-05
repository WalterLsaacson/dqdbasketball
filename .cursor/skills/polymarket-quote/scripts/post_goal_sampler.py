"""Post-goal CLOB re-quote sampler (data only, no trading).

When a score_change triggers buy_win (dry_run/posted), record t=0 from that
quote and schedule five more book snapshots at +10s … +50s (6 total).

Jobs run in parallel threads so elapsed_s matches wall clock under load.
Follow-up samples re-read the match score and recompute settlement.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import quote_lib as lib

logger = logging.getLogger("pm_quote.post_goal_sampler")

SAMPLE_COUNT = 6
SAMPLE_INTERVAL_S = 10.0  # samples at 0,10,20,30,40,50s
BUY_STATUSES = frozenset({"dry_run", "posted"})

_TOTAL_KEY_RE = re.compile(
    r"^(match|home|away)(?:_(1h|2h))?_total_([0-9.]+)_(over|under)$",
    re.I,
)
_EXACT_KEY_RE = re.compile(r"^exact_(\d+)-(\d+)_(yes|no)$", re.I)
_BTTS_KEY_RE = re.compile(r"^btts(?:_(1h|2h))?_(yes|no)$", re.I)

_active: "PostGoalSampler | None" = None
_active_lock = threading.Lock()


def set_active_sampler(sampler: "PostGoalSampler | None") -> None:
    global _active
    with _active_lock:
        _active = sampler


def get_active_sampler() -> "PostGoalSampler | None":
    with _active_lock:
        return _active


def samples_path(root: Path) -> Path:
    return lib.data_dir(root) / "post_goal_samples.jsonl"


@dataclass
class _TokenSpec:
    token_id: str
    market_key: str
    family: str
    outcome: str
    settlement_t0: str
    question: str
    trade_status: str
    trade_live: bool
    plan_usdc: float | None
    plan_shares: float | None
    t0_best_ask: float | None
    t0_best_bid: float | None
    t0_net_edge: float | None
    t0_gross_edge: float | None
    t0_fee: float | None
    t0_misprice: bool
    # settlement helpers
    line: float | None = None
    total_side: str | None = None
    total_period: str | None = None
    is_over: bool | None = None
    btts_period: str | None = None
    is_btts_yes: bool | None = None
    exact_home: int | None = None
    exact_away: int | None = None
    is_exact_yes: bool | None = None


@dataclass
class _SampleJob:
    match_id: str
    event_key: str
    home: str
    away: str
    score_at_t0_home: Any
    score_at_t0_away: Any
    t0_iso: str
    t0_mono: float
    tokens: list[_TokenSpec]
    eps: float
    fee_rate: float
    min_net: float
    proxy: str | None | object = field(default=...)


class PostGoalSampler:
    """Background queue: re-fetch books for buy tokens; append jsonl only."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._q: queue.Queue[_SampleJob | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="post-goal-sampler", daemon=True
        )
        self._thread.start()
        set_active_sampler(self)
        logger.info(
            "post-goal sampler on → %s (interval=%ss count=%s parallel_jobs)",
            samples_path(self.root),
            SAMPLE_INTERVAL_S,
            SAMPLE_COUNT,
        )

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._workers_lock:
            workers = list(self._workers)
        for t in workers:
            t.join(timeout=1.0)
        if get_active_sampler() is self:
            set_active_sampler(None)

    def enqueue_from_bundle(
        self,
        bundle: dict[str, Any],
        *,
        eps: float,
        fee_rate: float,
        min_net: float,
        proxy: str | None | object = ...,
    ) -> int:
        """If score_change produced buy_win fills, write sample 0 and schedule rest."""
        if (bundle.get("trigger") or "") != "score_change":
            return 0
        if self._stop.is_set():
            return 0

        tokens: list[_TokenSpec] = []
        for q in bundle.get("quotes") or []:
            if str(q.get("trade") or "") != "buy_win":
                continue
            ta = q.get("trade_attempt") or {}
            if ta.get("status") not in BUY_STATUSES or not ta.get("success"):
                continue
            tid = str(q.get("token_id") or "")
            if not tid:
                continue
            plan = ta.get("plan") or {}
            tokens.append(_token_from_quote(q, ta, plan))
        if not tokens:
            return 0

        t0_iso = str(bundle.get("quoted_at") or lib.now_cn_iso())
        job = _SampleJob(
            match_id=str(bundle.get("match_id") or ""),
            event_key=str(bundle.get("event_key") or ""),
            home=str(bundle.get("home") or ""),
            away=str(bundle.get("away") or ""),
            score_at_t0_home=bundle.get("home_score"),
            score_at_t0_away=bundle.get("away_score"),
            t0_iso=t0_iso,
            t0_mono=time.monotonic(),
            tokens=tokens,
            eps=float(eps),
            fee_rate=float(fee_rate),
            min_net=float(min_net),
            proxy=proxy,
        )

        rev = self._reversal_state(
            match_id=job.match_id,
            after_iso=job.t0_iso,
            score_home=job.score_at_t0_home,
            score_away=job.score_at_t0_away,
        )
        score_now = (
            job.score_at_t0_home,
            job.score_at_t0_away,
            None,
            None,
        )
        rows = [
            self._row_from_t0(
                job, tok, sample_i=0, elapsed_s=0.0, reversal=rev, score_now=score_now
            )
            for tok in tokens
        ]
        lib.append_jsonl(samples_path(self.root), rows)
        self._q.put(job)
        logger.info(
            "post-goal sample queued match=%s tokens=%d event=%s…",
            job.match_id,
            len(tokens),
            (job.event_key or "")[:48],
        )
        return len(tokens)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is None:
                return
            # One thread per job so elapsed_s tracks wall clock under concurrent buys.
            t = threading.Thread(
                target=self._run_job_safe,
                args=(job,),
                name=f"post-goal-job-{job.match_id}",
                daemon=True,
            )
            with self._workers_lock:
                self._workers = [w for w in self._workers if w.is_alive()]
                self._workers.append(t)
            t.start()

    def _run_job_safe(self, job: _SampleJob) -> None:
        try:
            self._run_job(job)
        except Exception:  # noqa: BLE001
            logger.exception("post-goal sample job failed match=%s", job.match_id)

    def _run_job(self, job: _SampleJob) -> None:
        for sample_i in range(1, SAMPLE_COUNT):
            if self._stop.is_set():
                return
            target = job.t0_mono + sample_i * SAMPLE_INTERVAL_S
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= target:
                    break
                time.sleep(min(0.25, target - now))
            if self._stop.is_set():
                return
            elapsed = round(time.monotonic() - job.t0_mono, 3)
            rev = self._reversal_state(
                match_id=job.match_id,
                after_iso=job.t0_iso,
                score_home=job.score_at_t0_home,
                score_away=job.score_at_t0_away,
            )
            score_now = self._current_score(job.match_id)
            rows = self._sample_books(
                job,
                sample_i=sample_i,
                elapsed_s=elapsed,
                reversal=rev,
                score_now=score_now,
            )
            if rows:
                lib.append_jsonl(samples_path(self.root), rows)

    def _sample_books(
        self,
        job: _SampleJob,
        *,
        sample_i: int,
        elapsed_s: float,
        reversal: dict[str, Any],
        score_now: tuple[Any, Any, Any, Any],
    ) -> list[dict[str, Any]]:
        ids = [t.token_id for t in job.tokens]
        try:
            books = lib.fetch_books(ids, proxy=job.proxy)
        except Exception as e:  # noqa: BLE001
            logger.warning("post-goal fetch_books failed: %s", e)
            books = {}
        out: list[dict[str, Any]] = []
        sampled_at = lib.now_cn_iso()
        hs, aws, hh, ah = score_now
        for tok in job.tokens:
            book = books.get(tok.token_id) or {
                "book_missing": True,
                "best_bid": None,
                "best_ask": None,
            }
            settlement, locked = settle_token_at_score(
                tok, home_score=hs, away_score=aws, home_half=hh, away_half=ah
            )
            # Unlocked / unknown → do not pretend still-mispriced buy.
            if settlement is None or not locked:
                mis, reason, econ = (
                    False,
                    "unlocked_or_unknown_settlement",
                    {
                        "gross_edge": None,
                        "fee": None,
                        "net_edge": None,
                        "trade": None,
                    },
                )
            else:
                mis, reason, econ = lib.flag_misprice(
                    settlement,
                    book,
                    eps=job.eps,
                    fee_rate=job.fee_rate,
                    min_net=job.min_net,
                )
            out.append(
                self._sample_row(
                    job,
                    tok,
                    sample_i=sample_i,
                    elapsed_s=elapsed_s,
                    sampled_at=sampled_at,
                    book=book,
                    settlement=settlement,
                    locked=locked,
                    mis=mis,
                    reason=reason,
                    econ=econ,
                    reversal=reversal,
                    score_now=score_now,
                )
            )
        return out

    def _row_from_t0(
        self,
        job: _SampleJob,
        tok: _TokenSpec,
        *,
        sample_i: int,
        elapsed_s: float,
        reversal: dict[str, Any],
        score_now: tuple[Any, Any, Any, Any],
    ) -> dict[str, Any]:
        return self._sample_row(
            job,
            tok,
            sample_i=sample_i,
            elapsed_s=elapsed_s,
            sampled_at=job.t0_iso,
            book={
                "best_bid": tok.t0_best_bid,
                "best_ask": tok.t0_best_ask,
                "best_bid_size": None,
                "best_ask_size": None,
                "book_missing": False,
            },
            settlement=tok.settlement_t0,
            locked=True,
            mis=tok.t0_misprice,
            reason=None,
            econ={
                "gross_edge": tok.t0_gross_edge,
                "fee": tok.t0_fee,
                "net_edge": tok.t0_net_edge,
                "trade": "buy_win",
            },
            reversal=reversal,
            score_now=score_now,
        )

    def _sample_row(
        self,
        job: _SampleJob,
        tok: _TokenSpec,
        *,
        sample_i: int,
        elapsed_s: float,
        sampled_at: str,
        book: dict[str, Any],
        settlement: str | None,
        locked: bool,
        mis: bool,
        reason: str | None,
        econ: dict[str, Any],
        reversal: dict[str, Any],
        score_now: tuple[Any, Any, Any, Any],
    ) -> dict[str, Any]:
        hs, aws, _, _ = score_now
        return {
            "sampled_at": sampled_at,
            "sample_i": sample_i,
            "elapsed_s": elapsed_s,
            "sample_count": SAMPLE_COUNT,
            "sample_interval_s": SAMPLE_INTERVAL_S,
            "match_id": job.match_id,
            "event_key": job.event_key,
            "home": job.home,
            "away": job.away,
            "score_at_t0": [job.score_at_t0_home, job.score_at_t0_away],
            "score_at_sample": [hs, aws],
            "t0_quoted_at": job.t0_iso,
            "token_id": tok.token_id,
            "market_key": tok.market_key,
            "family": tok.family,
            "outcome": tok.outcome,
            "settlement_t0": tok.settlement_t0,
            "settlement": settlement,
            "settlement_changed": settlement != tok.settlement_t0,
            "locked": locked,
            "question": tok.question,
            "trade_status": tok.trade_status,
            "trade_live": tok.trade_live,
            "plan_usdc": tok.plan_usdc,
            "plan_shares": tok.plan_shares,
            "best_bid": book.get("best_bid"),
            "best_ask": book.get("best_ask"),
            "best_bid_size": book.get("best_bid_size"),
            "best_ask_size": book.get("best_ask_size"),
            "book_missing": bool(book.get("book_missing")),
            "misprice": mis,
            "misprice_reason": reason,
            "gross_edge": econ.get("gross_edge"),
            "fee": econ.get("fee"),
            "net_edge": econ.get("net_edge"),
            "trade": econ.get("trade"),
            "t0_best_ask": tok.t0_best_ask,
            "t0_best_bid": tok.t0_best_bid,
            "t0_net_edge": tok.t0_net_edge,
            "t0_misprice": tok.t0_misprice,
            "reversal_seen": bool(reversal.get("seen")),
            "reversal_ts": reversal.get("ts"),
            "reversal_delta_s": reversal.get("delta_s"),
            "reversal_path": reversal.get("path"),
        }

    def _current_score(self, match_id: str) -> tuple[Any, Any, Any, Any]:
        """Return (home, away, home_half, away_half) best-effort from bridge state."""
        if not match_id:
            return None, None, None, None
        try:
            for m in lib.load_bridge_matches(self.root):
                dqd = m.get("dongqiudi") or {}
                if str(dqd.get("id") or "") != str(match_id):
                    continue
                return (
                    dqd.get("home_score"),
                    dqd.get("away_score"),
                    dqd.get("home_half") or dqd.get("home_score_half"),
                    dqd.get("away_half") or dqd.get("away_score_half"),
                )
        except Exception:  # noqa: BLE001
            pass
        # Fallback: last score_change / match_finished in events.jsonl
        path = lib.bridge_dir(self.root) / "events.jsonl"
        if not path.is_file():
            return None, None, None, None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-2000:]
        except OSError:
            return None, None, None, None
        hs = aws = None
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if str(ev.get("match_id") or "") != str(match_id):
                continue
            typ = ev.get("type") or ""
            if typ == "score_change":
                curr = ev.get("curr") or {}
                return curr.get("home"), curr.get("away"), None, None
            if typ == "match_finished":
                return ev.get("home_score"), ev.get("away_score"), None, None
        return hs, aws, None, None

    def _reversal_state(
        self,
        *,
        match_id: str,
        after_iso: str,
        score_home: Any,
        score_away: Any,
    ) -> dict[str, Any]:
        """Only count reversals that undo the t0 score (prev == score_at_t0)."""
        empty = {"seen": False, "ts": None, "delta_s": None, "path": None}
        if not match_id:
            return empty
        path = lib.bridge_dir(self.root) / "events.jsonl"
        if not path.is_file():
            return empty
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-3000:]
        except OSError:
            return empty

        after_t = _parse_iso(after_iso)
        t0_pair = (score_home, score_away)
        best: dict[str, Any] | None = None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if not isinstance(ev, dict):
                continue
            if str(ev.get("match_id") or "") != str(match_id):
                continue
            if (ev.get("type") or "") != "score_change":
                continue
            ts = ev.get("ts") or ""
            ev_t = _parse_iso(str(ts))
            if after_t and ev_t and ev_t <= after_t:
                continue
            prev = ev.get("prev") or {}
            curr = ev.get("curr") or {}
            # Must specifically undo the score we bought into.
            if (prev.get("home"), prev.get("away")) != t0_pair:
                continue
            if not _score_less(curr.get("home"), curr.get("away"), score_home, score_away):
                continue
            delta = None
            if after_t and ev_t:
                delta = round((ev_t - after_t).total_seconds(), 3)
            cand = {
                "seen": True,
                "ts": ts,
                "delta_s": delta,
                "path": (
                    f"{prev.get('home')}-{prev.get('away')}→"
                    f"{curr.get('home')}-{curr.get('away')}"
                ),
            }
            if best is None or (
                delta is not None
                and (best.get("delta_s") is None or delta < best["delta_s"])
            ):
                best = cand
        return best or empty


def _token_from_quote(q: dict[str, Any], ta: dict[str, Any], plan: dict[str, Any]) -> _TokenSpec:
    market_key = str(q.get("market_key") or "")
    family = str(q.get("family") or "")
    outcome = str(q.get("outcome") or "")
    line = _f(q.get("line"))
    total_side = q.get("total_side")
    total_period = q.get("total_period")
    is_over = None
    btts_period = q.get("btts_period")
    is_btts_yes = None
    exact_home = exact_away = None
    is_exact_yes = None

    if family == "totals" or "total_" in market_key:
        m = _TOTAL_KEY_RE.match(market_key)
        if m:
            total_side = total_side or m.group(1).lower()
            total_period = total_period or (m.group(2) or "ft").lower()
            if line is None:
                line = _f(m.group(3))
            is_over = m.group(4).lower() == "over"
        if is_over is None:
            is_over = "over" in outcome.lower()
        family = family or "totals"

    if family == "btts" or market_key.startswith("btts"):
        m = _BTTS_KEY_RE.match(market_key)
        if m:
            btts_period = btts_period or (m.group(1) or "ft")
            is_btts_yes = m.group(2).lower() == "yes"
        if is_btts_yes is None:
            is_btts_yes = outcome.lower() == "yes"
        family = family or "btts"

    if family == "exact_score" or market_key.startswith("exact_"):
        m = _EXACT_KEY_RE.match(market_key)
        if m:
            exact_home, exact_away = int(m.group(1)), int(m.group(2))
            is_exact_yes = m.group(3).lower() == "yes"
        if is_exact_yes is None:
            is_exact_yes = outcome.lower() == "yes"
        family = family or "exact_score"

    return _TokenSpec(
        token_id=str(q.get("token_id") or ""),
        market_key=market_key,
        family=family,
        outcome=outcome,
        settlement_t0=str(q.get("settlement") or ""),
        question=str(q.get("question") or ""),
        trade_status=str(ta.get("status") or ""),
        trade_live=bool(ta.get("live")),
        plan_usdc=_f(plan.get("usdc")),
        plan_shares=_f(plan.get("shares")),
        t0_best_ask=_f(q.get("best_ask")),
        t0_best_bid=_f(q.get("best_bid")),
        t0_net_edge=_f(q.get("net_edge")),
        t0_gross_edge=_f(q.get("gross_edge")),
        t0_fee=_f(q.get("fee")),
        t0_misprice=bool(q.get("misprice")),
        line=line,
        total_side=str(total_side).lower() if total_side else None,
        total_period=str(total_period).lower() if total_period else None,
        is_over=is_over,
        btts_period=str(btts_period).lower() if btts_period else None,
        is_btts_yes=is_btts_yes,
        exact_home=exact_home,
        exact_away=exact_away,
        is_exact_yes=is_exact_yes,
    )


def settle_token_at_score(
    tok: _TokenSpec,
    *,
    home_score: Any,
    away_score: Any,
    home_half: Any = None,
    away_half: Any = None,
) -> tuple[str | None, bool]:
    """Return (settlement, locked) at the given score. None = unknown/unlocked."""
    family = (tok.family or "").lower()

    if family == "totals":
        if tok.line is None or not tok.total_side:
            return tok.settlement_t0 or None, True
        period = tok.total_period or "ft"
        goals = lib._goals_for_total(  # noqa: SLF001 — shared settle helper
            side=tok.total_side,
            period=period,
            home_score=home_score,
            away_score=away_score,
            home_half=home_half,
            away_half=away_half,
        )
        if goals is None:
            return None, False
        # Live lock for Over: only when goals already exceed line.
        locked = goals > float(tok.line)
        if not locked:
            # Under of an unlocked total is not a live locked market either.
            return None, False
        over_wins = goals > float(tok.line)
        is_over = bool(tok.is_over)
        settlement = (
            "WIN"
            if (is_over and over_wins) or ((not is_over) and (not over_wins))
            else "LOSE"
        )
        return settlement, True

    if family == "btts":
        period = tok.btts_period or "ft"
        both = lib._btts_both_scored(  # noqa: SLF001
            period=period,
            home_score=home_score,
            away_score=away_score,
            home_half=home_half,
            away_half=away_half,
        )
        if both is None:
            return None, False
        if not both:
            return None, False  # live: Yes not locked yet
        is_yes = bool(tok.is_btts_yes)
        settlement = "WIN" if (is_yes and both) or ((not is_yes) and (not both)) else "LOSE"
        return settlement, True

    if family == "exact_score":
        if tok.exact_home is None or tok.exact_away is None:
            return tok.settlement_t0 or None, True
        try:
            h, a = int(home_score), int(away_score)
        except (TypeError, ValueError):
            return None, False
        sh, sa = int(tok.exact_home), int(tok.exact_away)
        # Live: locked dead scoreline when curr already exceeded printed line.
        if sh < h or sa < a:
            yes_wins = False
            locked = True
        else:
            # Still reachable — not locked for live buy of No on other lines either
            # unless this is FT. Treat as unlocked for sampling.
            return None, False
        is_yes = bool(tok.is_exact_yes)
        settlement = "WIN" if (is_yes and yes_wins) or ((not is_yes) and (not yes_wins)) else "LOSE"
        return settlement, locked

    # Unknown family: keep t0 settlement (best effort).
    return tok.settlement_t0 or None, True


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(s: str):
    from datetime import datetime

    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _score_less(h1: Any, a1: Any, h0: Any, a0: Any) -> bool:
    try:
        return (int(h1) + int(a1)) < (int(h0) + int(a0))
    except (TypeError, ValueError):
        return False

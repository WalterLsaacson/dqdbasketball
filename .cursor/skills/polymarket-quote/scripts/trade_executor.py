"""Execute fills right after misprice flag (in-process, no JSONL consume)."""

from __future__ import annotations

import json
import logging
import re
import threading
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import quote_lib as lib
from clob_trader import ClobTrader
from fill_planner import FillPlan, plan_fill
from size_policy import compute_buy_size_caps
from score_reversal import (
    AF_STATUS_CONFIRMED,
    AF_STATUS_NONE,
    AF_STATUS_PENDING,
    FILL_STATUS_OPEN,
    FILL_STATUS_PENDING,
    OpenPositionLedger,
    deadline_iso,
    entry_tuple,
    event_signals_reversal,
    ft_reversal_vs_entry,
    lot_depends_on_disallowed_goal,
    score_pair,
)
from trade_settings import TradeSettings

logger = logging.getLogger("pm_quote.trade")

# CLOB FAK/FOK sell: maker (shares) max 2 decimals; floor to avoid invalid maker amount.
FLATTEN_SHARE_DECIMALS = 2
FLATTEN_MIN_SHARES = Decimal("0.01")
# Emergency flatten floor price (do not dump into sub-0.2 bids forever).
FLATTEN_MIN_PRICE = Decimal("0.5")
# If 0.2 FAK leaves the full bag, one dump attempt at the tick floor.
FLATTEN_DUMP_MIN_PRICE = Decimal("0.01")
# Polymarket matched-order cache often rejects 100% sells; keep a haircut.
FLATTEN_SELL_HAIRCUT = Decimal("0.99")
# Live bal vs gate bal: within this → trust gate "free"; else size from live only.
FLATTEN_GATE_BAL_EPS = Decimal("0.02")
# Hard stop: zombie pending_flatten loops (resolved markets / never-filled buys).
FLATTEN_MAX_ATTEMPTS = 60
# Delayed buy never shows balance → abandon (don't retry forever).
FLATTEN_DELAYED_FILL_MAX_ATTEMPTS = 30
# Keep pending_reason bounded (append loops used to grow to 80KB+).
FLATTEN_REASON_MAX_LEN = 400
_TERMINAL_FLATTEN_ERR_RE = re.compile(
    r"invalid\s+maker\s+amount|invalid\s+amounts|invalid\s+token\s+id",
    re.IGNORECASE,
)
_BALANCE_GATE_RE = re.compile(
    r"balance:\s*(\d+).*?sum of matched orders:\s*(\d+).*?order amount[^:]*:\s*(\d+)",
    re.IGNORECASE | re.DOTALL,
)
_NOT_ENOUGH_BAL_RE = re.compile(
    r"not enough balance\s*/\s*allowance",
    re.IGNORECASE,
)


def trade_idempotency_key(event_key: str, token_id: str, trade: str) -> str:
    return f"{event_key}|{token_id}|{trade}"


def floor_shares(shares: Decimal | float | str, *, decimals: int = FLATTEN_SHARE_DECIMALS) -> Decimal:
    """Round shares down to ``decimals`` (CLOB sell maker precision)."""
    q = Decimal(10) ** -int(decimals)
    d = Decimal(str(shares))
    if d <= 0:
        return Decimal("0")
    return (d / q).to_integral_value(rounding=ROUND_DOWN) * q


def flatten_sell_shares(bal: Decimal) -> Decimal:
    """Shares to FAK-sell: 2dp floor with 99% haircut (PM balance-gate workaround)."""
    bal = Decimal(str(bal))
    if bal <= 0:
        return Decimal("0")
    haircut = floor_shares(bal * FLATTEN_SELL_HAIRCUT)
    if haircut >= FLATTEN_MIN_SHARES:
        return haircut
    # Tiny lots: leave 0.01 dust when possible, else full floor.
    full = floor_shares(bal)
    leave = full - Decimal("0.01")
    if leave >= FLATTEN_MIN_SHARES:
        return leave
    return full


def flatten_sell_shares_available(
    bal: Decimal,
    *,
    free: Decimal | None = None,
) -> Decimal:
    """Haircut sell size, capped by free (unlocked) shares when known."""
    bal = Decimal(str(bal))
    sized = flatten_sell_shares(bal)
    if free is None:
        return sized
    free_d = Decimal(str(free))
    if free_d <= 0:
        return Decimal("0")
    capped = floor_shares(free_d)
    if capped < FLATTEN_MIN_SHARES:
        return Decimal("0")
    return min(sized, capped)


def parse_balance_gate_error(err: str | Exception | None) -> dict[str, Decimal] | None:
    """Parse CLOB 'not enough balance' micro-unit fields → Decimal shares."""
    if err is None:
        return None
    text = str(err)
    if not _NOT_ENOUGH_BAL_RE.search(text):
        return None
    m = _BALANCE_GATE_RE.search(text)
    if not m:
        return None
    scale = Decimal("1000000")
    bal = Decimal(m.group(1)) / scale
    matched = Decimal(m.group(2)) / scale
    order_amt = Decimal(m.group(3)) / scale
    return {
        "balance": bal,
        "matched": matched,
        "order_amount": order_amt,
        "free": bal - matched,
    }


def gate_has_locked_inventory(gate: dict[str, Decimal] | None) -> bool:
    """True when matched orders leave nothing tradeable (do not dust-close)."""
    if not gate:
        return False
    matched = Decimal(str(gate.get("matched") or 0))
    bal = Decimal(str(gate.get("balance") or 0))
    free = Decimal(str(gate.get("free") or 0))
    # Nothing free to sell, but matched and/or bag still look real.
    if free < FLATTEN_MIN_SHARES and matched >= FLATTEN_MIN_SHARES:
        return True
    if free < FLATTEN_MIN_SHARES and bal >= FLATTEN_MIN_SHARES:
        return True
    return False


def gate_free_cap(
    gate: dict[str, Decimal] | None,
    live_bal: Decimal,
    *,
    eps: Decimal = FLATTEN_GATE_BAL_EPS,
) -> Decimal | None:
    """Return gate free shares only when live bal still matches the gate bag.

    After cancel/settle, if live balance moved, ignore stale free and size from
    live balance alone.
    """
    if not gate:
        return None
    try:
        gbal = Decimal(str(gate.get("balance") or 0))
        free = Decimal(str(gate.get("free") or 0))
    except Exception:  # noqa: BLE001
        return None
    live = Decimal(str(live_bal))
    if abs(live - gbal) > Decimal(str(eps)):
        return None
    return free


def is_not_enough_balance_error(err: str | Exception | None) -> bool:
    if err is None:
        return False
    return bool(_NOT_ENOUGH_BAL_RE.search(str(err)))


def is_terminal_flatten_error(err: str | Exception | None) -> bool:
    """Errors that will not succeed on blind retry (stop pending_flatten loop)."""
    if err is None:
        return False
    return bool(_TERMINAL_FLATTEN_ERR_RE.search(str(err)))


def flatten_reason_append(base: str, *parts: str) -> str:
    """Join reason fragments without unbounded growth from retry appends."""
    chunks: list[str] = []
    for p in (base, *parts):
        s = str(p or "").strip()
        if not s:
            continue
        # Drop repeated awaiting_delayed_fill / balance_gate_partial spam.
        if chunks and s in ("awaiting_delayed_fill", "balance_gate_partial"):
            if chunks[-1] == s or chunks[-1].endswith("|" + s):
                continue
        chunks.append(s)
    out = "|".join(chunks)
    if len(out) <= FLATTEN_REASON_MAX_LEN:
        return out
    return out[: FLATTEN_REASON_MAX_LEN - 3] + "..."


def signal_from_event_key(event_key: str) -> str:
    """First segment of event_key (score_change|… / match_finished|…)."""
    ek = (event_key or "").strip()
    if not ek:
        return ""
    return ek.split("|", 1)[0]


class TradeExecutor:
    """Plan → optional post → trades.jsonl; memory + file idempotency."""

    def __init__(
        self,
        root: Path,
        settings: TradeSettings,
        *,
        trader: ClobTrader | None = None,
        af_mode: str = "gate",
        af_timeout_s: float = 90.0,
    ) -> None:
        self.root = Path(root)
        self.settings = settings
        self.trader = trader
        mode = str(af_mode or "gate").strip().lower()
        if mode not in ("postcheck", "gate", "off"):
            mode = "gate"
        self.af_mode = mode
        self.af_timeout_s = max(1.0, float(af_timeout_s))
        self._done: set[str] = set()
        self._flatten_done: set[str] = set()
        # Matches with in-flight / pending exits — block new buy_win opens.
        self._buy_blocked_matches: set[str] = set()
        self._lock = threading.RLock()
        self.ledger = OpenPositionLedger(lib.data_dir(self.root) / "open_positions.json")
        self._load_recent_successes()
        self._load_recent_flattens()
        live_signals: set[str] = set()
        if self.settings.live_score:
            live_signals.add("score_change")
        if self.settings.live_prop:
            live_signals.add("player_stat_change")
        if self.settings.live_ft:
            live_signals.add("match_finished")
        if live_signals:
            both = live_signals >= {"score_change", "match_finished"}
            purged = self.ledger.purge_dry_run_opens_for_signals(
                live_signals,
                reason="pre_live_purge",
                purge_unknown=both,
            )
            if purged:
                logger.warning(
                    "purged %d dry-run open lots before live trading (%s)",
                    purged,
                    ",".join(sorted(live_signals)),
                )
        self._rebuild_open_from_trades()

    def _live_for_signal(self, event_type: str) -> bool:
        """Whether this bridge signal posts real CLOB orders."""
        typ = (event_type or "").strip()
        if typ == "score_change":
            return bool(self.settings.live_score)
        if typ == "player_stat_change":
            return bool(self.settings.live_prop)
        if typ == "match_finished":
            return bool(self.settings.live_ft)
        return bool(self.settings.live)

    def _resolve_event_type(
        self,
        *,
        event_type: str = "",
        event_key: str = "",
        match_meta: dict[str, Any] | None = None,
    ) -> str:
        typ = (event_type or "").strip()
        if not typ and match_meta:
            typ = str(match_meta.get("event_type") or "").strip()
        if not typ:
            typ = signal_from_event_key(event_key)
        return typ

    def _keep_buy_for_rebuild(self, row: dict[str, Any]) -> bool:
        """Keep buy_win row as open lot for the channel's current dry/live mode."""
        if not row.get("success"):
            return False
        sig = signal_from_event_key(str(row.get("event_key") or ""))
        channel_live = self._live_for_signal(sig)
        if channel_live:
            return (
                row.get("status") == "posted"
                and bool(row.get("live"))
            )
        return row.get("status") in ("dry_run", "posted")

    def _rebuild_open_from_trades(self, limit: int = 800) -> None:
        """Re-open buy_win lots from trades.jsonl that were never flattened (restart-safe)."""
        path = self.trades_path
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return
        flattened: set[tuple[str, str]] = set()
        buys: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            mid = str(row.get("match_id") or "")
            tid = str(row.get("token_id") or "")
            if row.get("trade") == "flatten_reversal" and row.get("success"):
                if mid and tid:
                    flattened.add((mid, tid))
                continue
            if row.get("trade") != "buy_win":
                continue
            if not self._keep_buy_for_rebuild(row):
                continue
            if "verify" in str(row.get("idempotency_key") or ""):
                continue
            buys.append(row)
        # Last buy per match+token wins
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in buys:
            mid = str(row.get("match_id") or "")
            tid = str(row.get("token_id") or "")
            if mid and tid:
                latest[(mid, tid)] = row
        for (mid, tid), row in latest.items():
            if (mid, tid) in flattened:
                continue
            if any(
                str(x.get("token_id")) == tid
                for x in self.ledger.open_for_match(mid)
            ):
                continue
            plan = row.get("plan") or {}
            shares = float(plan.get("shares") or 0)
            if shares <= 0:
                continue
            self.ledger.register_buy(
                match_id=mid,
                token_id=tid,
                market_key=str(row.get("market_key") or ""),
                shares=shares,
                usdc=float(plan.get("usdc") or 0),
                home_score=row.get("home_score"),
                away_score=row.get("away_score"),
                live=bool(row.get("live")),
                event_key=str(row.get("event_key") or ""),
                home=str(row.get("home") or ""),
                away=str(row.get("away") or ""),
                family=str(row.get("family") or ""),
                tick_size="0.01",
                neg_risk=None,
            )
        self._close_stale_finished_lots()

    def _close_stale_finished_lots(self) -> None:
        """Drop open lots for matches that already finished (free open-budget).

        Also closes FT-vs-entry reversals (false-goal leftovers). Finished winners
        must not keep blocking ``QUOTE_MAX_OPEN_USDC`` after the game ends.
        """
        latest_ft: dict[str, tuple[int, int]] = {}
        try:
            for m in lib.load_bridge_matches(self.root):
                dqd = m.get("dongqiudi") or {}
                mid = str(dqd.get("id") or "")
                sc = score_pair(dqd.get("home_score"), dqd.get("away_score"))
                st = str(dqd.get("status") or "").lower()
                finished = bool(dqd.get("is_finished")) or st in (
                    "played",
                    "finished",
                    "ft",
                ) or "play" == st
                # Dongqiudi often uses status display "Played"
                if "played" in st or finished:
                    if mid and sc:
                        latest_ft[mid] = sc
        except Exception as e:  # noqa: BLE001
            logger.warning("stale FT scan matches.json failed: %s", e)

        # Peek finished scores from events.jsonl (matches drop off matches.json).
        try:
            for line in (lib.bridge_dir(self.root) / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()[-50000:]:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if (ev.get("type") or "") != "match_finished":
                    continue
                mid = str(ev.get("match_id") or "")
                sc = score_pair(ev.get("home_score"), ev.get("away_score"))
                if mid and sc:
                    latest_ft[mid] = sc
        except OSError:
            pass

        closed = 0
        for lot in list(self.ledger.all_open()):
            mid = str(lot.get("match_id") or "")
            ft = latest_ft.get(mid)
            if not ft:
                continue
            if ft_reversal_vs_entry(entry=entry_tuple(lot), ft=ft):
                reason = f"stale_ft_reversal ft={ft[0]}-{ft[1]}"
            else:
                reason = f"stale_ft_settled ft={ft[0]}-{ft[1]}"
            self.ledger.mark_closed(
                str(lot.get("token_id")),
                mid,
                reason=reason,
            )
            closed += 1
        if closed:
            logger.info("closed %d stale finished open lots on rebuild", closed)


    @property
    def trades_path(self) -> Path:
        return lib.data_dir(self.root) / "trades.jsonl"

    def _load_recent_successes(self, limit: int = 500) -> None:
        path = self.trades_path
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            # Only live successful posts block future attempts across restarts.
            if (
                row.get("status") == "posted"
                and row.get("success")
                and row.get("idempotency_key")
            ):
                self._done.add(str(row["idempotency_key"]))

    def _load_recent_flattens(self, limit: int = 500) -> None:
        path = self.trades_path
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("trade") != "flatten_reversal":
                continue
            key = row.get("idempotency_key")
            if key and row.get("status") in ("flatten_dry_run", "flatten_posted") and row.get(
                "success"
            ):
                self._flatten_done.add(str(key))
            # Rebuild open ledger from successful live buys if ledger empty? skip — ledger file is source

    def ensure_trader(self) -> ClobTrader | None:
        """Initialize once and reuse (plan: watch 启动时 initialize)."""
        if not self.settings.private_key:
            return self.trader if self.trader and self.trader.ready else None
        if self.trader is None:
            self.trader = ClobTrader(self.settings)
        if not self.trader.ready:
            self.trader.initialize()
        return self.trader

    def _extreme_price_blocked(self, price: float | None) -> str | None:
        if self.settings.allow_extreme_prices or price is None:
            return None
        if price <= 0.01 + 1e-12:
            return f"extreme_price={price} (<=0.01)"
        if price >= 0.99 - 1e-12:
            return f"extreme_price={price} (>=0.99)"
        return None

    def _min_buy_price_blocked(self, price: float | None) -> str | None:
        """buy_win: require best_ask >= min_buy_price (default 0.92; 0=off)."""
        floor = float(getattr(self.settings, "min_buy_price", 0.0) or 0.0)
        if floor <= 0 or price is None:
            return None
        if price < floor - 1e-12:
            return f"buy_price_below_min={price}<{floor}"
        return None

    def maybe_trade(
        self,
        quote: dict[str, Any],
        *,
        event_key: str = "",
        match_meta: dict[str, Any] | None = None,
        event_type: str = "",
    ) -> dict[str, Any] | None:
        """If quote is a misprice opportunity, plan and optionally post."""
        with self._lock:
            return self._maybe_trade_locked(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                event_type=event_type,
            )

    def _maybe_trade_locked(
        self,
        quote: dict[str, Any],
        *,
        event_key: str = "",
        match_meta: dict[str, Any] | None = None,
        event_type: str = "",
    ) -> dict[str, Any] | None:
        """Caller must hold ``self._lock`` (serializes ledger / _done / posts)."""
        if not self.settings.enabled:
            return None
        if not quote.get("misprice"):
            return None

        trade = str(quote.get("trade") or "")
        token_id = str(quote.get("token_id") or "")
        if not trade or not token_id:
            return None

        typ = self._resolve_event_type(
            event_type=event_type,
            event_key=event_key,
            match_meta=match_meta,
        )
        channel_live = self._live_for_signal(typ)

        key = trade_idempotency_key(event_key or "", token_id, trade)
        if key in self._done:
            logger.debug("skip duplicate %s", key)
            return {
                "idempotency_key": key,
                "status": "skipped",
                "skip_reason": "already_done",
            }

        # Price guard on the reference book price (best ask/bid)
        ref_price = quote.get("best_ask") if trade == "buy_win" else quote.get("best_bid")
        try:
            ref_f = float(ref_price) if ref_price is not None else None
        except (TypeError, ValueError):
            ref_f = None
        extreme = self._extreme_price_blocked(ref_f)
        if extreme:
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=None,
                status="skipped",
                skip_reason=extreme,
                response=None,
                success=False,
                idempotency_key=key,
                live=channel_live,
            )
            return row

        if trade == "buy_win":
            mid_block = str(
                (match_meta or {}).get("match_id")
                or quote.get("match_id")
                or ""
            )
            if mid_block and (
                mid_block in self._buy_blocked_matches
                or any(
                    r.get("pending_flatten")
                    for r in self.ledger.open_for_match(mid_block)
                )
            ):
                row = self._record(
                    quote,
                    event_key=event_key,
                    match_meta=match_meta,
                    plan=None,
                    status="skipped",
                    skip_reason="buy_blocked_pending_flatten",
                    response=None,
                    success=False,
                    idempotency_key=key,
                    live=channel_live,
                )
                return row
            below_min = self._min_buy_price_blocked(ref_f)
            if below_min:
                row = self._record(
                    quote,
                    event_key=event_key,
                    match_meta=match_meta,
                    plan=None,
                    status="skipped",
                    skip_reason=below_min,
                    response=None,
                    success=False,
                    idempotency_key=key,
                    live=channel_live,
                )
                self._done.add(key)
                logger.info(
                    "skip buy_win %s ask=%s (%s)",
                    (quote.get("market_key") or "")[:40],
                    ref_f,
                    below_min,
                )
                return row

        available: float | None = None
        if trade == "sell_lose":
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=None,
                status="skipped",
                skip_reason="sell_lose_disabled",
                response=None,
                success=False,
                idempotency_key=key,
                live=channel_live,
            )
            self._done.add(key)
            return row

        max_usdc = float(self.settings.max_usdc)
        max_shares = float(self.settings.max_shares)
        size_meta: dict[str, Any] | None = None
        if trade == "buy_win":
            open_usdc = sum(
                float(r.get("usdc") or 0)
                for r in self.ledger.all_open()
            )
            caps = compute_buy_size_caps(
                ref_f,
                max_usdc=self.settings.max_usdc,
                max_shares=self.settings.max_shares,
                tiers=self.settings.size_tiers,
                open_usdc=open_usdc,
                max_open_usdc=self.settings.max_open_usdc,
                floor_usdc=self.settings.size_floor_usdc,
            )
            size_meta = caps.to_dict()
            if caps.skip_reason:
                row = self._record(
                    quote,
                    event_key=event_key,
                    match_meta=match_meta,
                    plan=None,
                    status="skipped",
                    skip_reason=caps.skip_reason,
                    response=None,
                    success=False,
                    idempotency_key=key,
                    live=channel_live,
                    extra={"size_policy": size_meta},
                )
                return row
            max_usdc = float(caps.max_usdc)
            max_shares = float(caps.max_shares)
            logger.info(
                "size_policy ask=%.3f tier=%.2f eff_usdc=%.2f eff_shares=%.2f "
                "open=%.2f remaining=%s",
                caps.ask,
                caps.tier_usdc,
                caps.max_usdc,
                caps.max_shares,
                caps.open_usdc,
                caps.remaining_open,
            )

        plan = plan_fill(
            quote,
            take_depth=self.settings.take_depth,
            max_levels=self.settings.max_levels,
            max_usdc=max_usdc,
            max_shares=max_shares,
            max_slippage=self.settings.max_slippage,
            min_order_shares=self.settings.min_order_shares,
            available_shares=available,
        )

        if plan.skip_reason:
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=plan,
                status="skipped",
                skip_reason=plan.skip_reason,
                response=None,
                success=False,
                idempotency_key=key,
                live=channel_live,
                extra={"size_policy": size_meta} if size_meta else None,
            )
            return row

        if not channel_live:
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=plan,
                status="dry_run",
                skip_reason=None,
                response=None,
                success=True,
                idempotency_key=key,
                live=False,
                extra={"size_policy": size_meta} if size_meta else None,
            )
            self._done.add(key)
            logger.info(
                "dry-run %s %s shares=%.4f usdc=%.4f worst=%.4f depth=%s signal=%s",
                trade,
                token_id[:12],
                plan.shares,
                plan.usdc,
                plan.worst_price,
                plan.take_depth,
                typ or "?",
            )
            if trade == "buy_win":
                self._register_open_buy(
                    quote, plan=plan, event_key=event_key, match_meta=match_meta, live=False
                )
            return row

        # Live post
        try:
            trader = self.ensure_trader()
            assert trader is not None
            tick = str(quote.get("tick_size") or "0.01") or "0.01"
            neg = quote.get("neg_risk")
            neg_risk = bool(neg) if neg is not None else None
            if plan.side == "BUY":
                response = trader.post_market_buy(
                    token_id,
                    Decimal(str(plan.usdc)),
                    tick,
                    Decimal(str(plan.worst_price)),
                    order_type=plan.order_type,
                    neg_risk=neg_risk,
                )
            else:
                response = trader.post_market_sell(
                    token_id,
                    Decimal(str(plan.shares)),
                    tick,
                    min_price=Decimal(str(plan.worst_price)),
                    order_type=plan.order_type,
                    neg_risk=neg_risk,
                )
            ok = trader.is_order_success(response)
            resp_status = (
                str((response or {}).get("status") or "").upper()
                if isinstance(response, dict)
                else ""
            )
            # DELAYED: CLOB accepted — one quick balance peek (no multi-second wait).
            ledger_plan = plan
            if ok and trade == "buy_win" and resp_status == "DELAYED":
                bal = trader.wait_conditional_balance(
                    token_id, min_shares=0.01, timeout_s=0.0, interval_s=0.05
                )
                if bal > 0:
                    ledger_plan = FillPlan(
                        trade=plan.trade,
                        side=plan.side,
                        take_depth=plan.take_depth,
                        order_type=plan.order_type,
                        shares=round(bal, 6),
                        usdc=plan.usdc,
                        worst_price=plan.worst_price,
                        levels_used=plan.levels_used,
                        levels=list(plan.levels),
                        skip_reason=plan.skip_reason,
                    )
                    logger.info(
                        "delayed buy confirmed token=%s… shares=%.4f (plan=%.4f)",
                        token_id[:12],
                        bal,
                        plan.shares,
                    )
                else:
                    logger.warning(
                        "delayed buy accepted but balance still 0 token=%s… "
                        "registering plan shares=%.4f for flatten safety",
                        token_id[:12],
                        plan.shares,
                    )
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=ledger_plan,
                status="posted",
                skip_reason=(
                    f"delayed|{resp_status.lower()}" if resp_status == "DELAYED" else None
                ),
                response=response,
                success=ok,
                idempotency_key=key,
                live=True,
                extra={"size_policy": size_meta} if size_meta else None,
            )
            if ok:
                self._done.add(key)
                if trade == "buy_win":
                    # Must register even when delayed — otherwise score-reversal
                    # flatten never sees the lot (Alianza Over 4.5 bug).
                    fill_st = FILL_STATUS_OPEN
                    if resp_status == "DELAYED" and ledger_plan is plan:
                        # Balance still 0 — pending until shares appear / flatten sees bal.
                        fill_st = FILL_STATUS_PENDING
                    self._register_open_buy(
                        quote,
                        plan=ledger_plan,
                        event_key=event_key,
                        match_meta=match_meta,
                        live=True,
                        fill_status=fill_st,
                    )
            return row
        except Exception as e:  # noqa: BLE001
            logger.exception("live order failed: %s", e)
            row = self._record(
                quote,
                event_key=event_key,
                match_meta=match_meta,
                plan=plan,
                status="error",
                skip_reason=str(e),
                response=None,
                success=False,
                idempotency_key=key,
                live=True,
                extra={"size_policy": size_meta} if size_meta else None,
            )
            return row

    def _register_open_buy(
        self,
        quote: dict[str, Any],
        *,
        plan: FillPlan,
        event_key: str,
        match_meta: dict[str, Any] | None,
        live: bool,
        fill_status: str = FILL_STATUS_OPEN,
    ) -> None:
        meta = match_meta or {}
        mid = str(meta.get("match_id") or quote.get("match_id") or "")
        if not mid:
            return
        neg = quote.get("neg_risk")
        sig = signal_from_event_key(event_key)
        af_status = AF_STATUS_NONE
        af_deadline = None
        if sig == "score_change" and self.af_mode == "postcheck":
            af_status = AF_STATUS_PENDING
            af_deadline = deadline_iso(self.af_timeout_s)
        elif sig == "score_change" and self.af_mode == "gate":
            # Bought only after AF confirm.
            af_status = AF_STATUS_CONFIRMED
        self.ledger.register_buy(
            match_id=mid,
            token_id=str(quote.get("token_id") or ""),
            market_key=str(quote.get("market_key") or ""),
            shares=float(plan.shares),
            usdc=float(plan.usdc),
            home_score=meta.get("home_score"),
            away_score=meta.get("away_score"),
            live=live,
            event_key=event_key,
            home=str(meta.get("home") or ""),
            away=str(meta.get("away") or ""),
            family=str(quote.get("family") or ""),
            tick_size=str(quote.get("tick_size") or "0.01"),
            neg_risk=bool(neg) if neg is not None else None,
            af_status=af_status,
            af_deadline=af_deadline,
            fill_status=fill_status,
        )

    def mark_af_confirmed(self, match_id: str, *, event_key: str = "") -> int:
        with self._lock:
            return self.ledger.mark_af_confirmed(match_id, event_key=event_key)

    def refresh_af_deadline(
        self,
        match_id: str,
        *,
        event_key: str = "",
        timeout_s: float | None = None,
    ) -> int:
        """Align lot af_deadline with AF submit time (not buy time)."""
        with self._lock:
            return self.ledger.refresh_af_deadline(
                match_id,
                event_key=event_key,
                timeout_s=self.af_timeout_s if timeout_s is None else float(timeout_s),
            )

    def flatten_af_unconfirmed(
        self,
        match_id: str,
        *,
        event_key: str = "",
        reason: str = "af_confirm_timeout",
    ) -> list[dict[str, Any]]:
        """Flatten open lots still af_pending for this goal event_key."""
        with self._lock:
            return self._flatten_af_pending_locked(
                match_id=str(match_id),
                event_key=str(event_key or ""),
                reason=reason,
            )

    def flatten_af_deadline_lots(
        self,
        *,
        exclude_event_keys: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Flatten af_pending lots whose af_deadline has passed (drain safety net).

        Skip ``exclude_event_keys`` (typically still in-flight AF confirms) so we
        never sell a lot that AF is about to confirm on this tick.
        """
        with self._lock:
            if self.af_mode != "postcheck" or not self.settings.enabled:
                return []
            overdue = self.ledger.overdue_af_pending_lots(
                exclude_event_keys=exclude_event_keys
            )
            if not overdue:
                return []
            out: list[dict[str, Any]] = []
            # Group by match+event_key to emit stable flatten keys.
            seen_lots: set[tuple[str, str]] = set()
            for lot in overdue:
                mid = str(lot.get("match_id") or "")
                tid = str(lot.get("token_id") or "")
                if not mid or not tid or (mid, tid) in seen_lots:
                    continue
                seen_lots.add((mid, tid))
                ek = str(lot.get("event_key") or "")
                reason = "af_confirm_timeout"
                flatten_ek = (
                    f"flatten_af_deadline|{mid}|{ek}|{lib.now_cn_iso()}"
                )
                out.append(
                    self._flatten_lot(
                        lot,
                        event_key=flatten_ek,
                        reason=reason,
                        match_ev={"match_id": mid, "type": "score_change"},
                    )
                )
            return out

    def _flatten_af_pending_locked(
        self,
        *,
        match_id: str,
        event_key: str,
        reason: str,
    ) -> list[dict[str, Any]]:
        if not self.settings.enabled:
            return []
        mid = str(match_id)
        if not mid:
            return []
        lots = self.ledger.af_pending_lots(
            match_id=mid,
            event_key=event_key or None,
        )
        if not lots:
            return []
        logger.warning(
            "af-unconfirmed flatten match=%s lots=%d reason=%s event_key=%s",
            mid,
            len(lots),
            reason,
            event_key or "*",
        )
        out: list[dict[str, Any]] = []
        for lot in lots:
            flatten_ek = (
                f"flatten_af_timeout|{mid}|{lot.get('event_key') or event_key}|"
                f"{lib.now_cn_iso()}|{reason}"
            )
            out.append(
                self._flatten_lot(
                    lot,
                    event_key=flatten_ek,
                    reason=reason,
                    match_ev={"match_id": mid, "type": "score_change"},
                )
            )
        return out

    def maybe_flatten_for_event(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten only lots that depended on a disallowed goal (entry > after score)."""
        with self._lock:
            return self._maybe_flatten_for_event_locked(ev)

    def _maybe_flatten_for_event_locked(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.settings.enabled:
            return []
        mid = str(ev.get("match_id") or "")
        if not mid:
            return []

        open_lots = self.ledger.open_for_match(mid)
        if not open_lots:
            return []

        typ = ev.get("type") or ""
        after: tuple[int, int] | None = None
        reason = ""

        if typ == "score_change" and event_signals_reversal(ev):
            after = score_pair(ev.get("home_score"), ev.get("away_score"))
            prev = ev.get("prev") or {}
            reason = (
                f"score_reversal "
                f"{prev.get('home')}-{prev.get('away')}→"
                f"{ev.get('home_score')}-{ev.get('away_score')}"
            )
        elif typ == "match_finished":
            after = score_pair(ev.get("home_score"), ev.get("away_score"))
            reason = (
                f"ft_reversal_vs_entry ft={ev.get('home_score')}-{ev.get('away_score')}"
            )
        else:
            return []

        affected = [
            lot
            for lot in open_lots
            if lot_depends_on_disallowed_goal(lot, after_score=after)
        ]
        if not affected:
            return []

        # Block new buys for this match until exits clear.
        self._buy_blocked_matches.add(mid)
        logger.warning(
            "reversal flatten match=%s lots=%d/%d reason=%s",
            mid,
            len(affected),
            len(open_lots),
            reason,
        )
        out: list[dict[str, Any]] = []
        ek = f"flatten|{mid}|{ev.get('ts') or lib.now_cn_iso()}|{reason}"
        for lot in affected:
            out.append(self._flatten_lot(lot, event_key=ek, reason=reason, match_ev=ev))
        if not self.ledger.open_for_match(mid) and not self.ledger.pending_flatten_lots():
            self._buy_blocked_matches.discard(mid)
        elif not any(
            str(r.get("match_id")) == mid for r in self.ledger.pending_flatten_lots()
        ) and not self.ledger.open_for_match(mid):
            self._buy_blocked_matches.discard(mid)
        return out

    def retry_pending_flattens(self) -> list[dict[str, Any]]:
        """Retry live FAK exits that failed earlier (lot still open + pending_flatten)."""
        with self._lock:
            return self._retry_pending_flattens_locked()

    def _retry_pending_flattens_locked(self) -> list[dict[str, Any]]:
        if not self.settings.enabled:
            return []
        pending = self.ledger.pending_flatten_lots()
        if not pending:
            return []
        out: list[dict[str, Any]] = []
        for lot in pending:
            mid = str(lot.get("match_id") or "")
            tid = str(lot.get("token_id") or "")
            reason = str(lot.get("pending_reason") or "retry_pending_flatten")
            attempts = int(lot.get("flatten_attempts") or 0)
            # Stop looping terminal CLOB errors left in the ledger.
            # Delayed-fill timeout is handled inside _flatten_lot after a live
            # balance check (so late credits still get sold).
            give_up = ""
            if is_terminal_flatten_error(reason):
                give_up = "terminal_flatten_error"
            elif attempts >= FLATTEN_MAX_ATTEMPTS:
                give_up = f"max_attempts={attempts}"
            if give_up:
                self.ledger.mark_closed(
                    tid, mid, reason=flatten_reason_append(give_up, reason)
                )
                self._buy_blocked_matches.discard(mid)
                alert = (
                    f"ALERT flatten_give_up match={mid} token={tid[:12]}… "
                    f"{give_up} — stopped retry"
                )
                logger.error(alert)
                print(alert, flush=True)
                out.append(
                    {
                        "quoted_at": lib.now_cn_iso(),
                        "status": "flatten_abandoned",
                        "skip_reason": flatten_reason_append(give_up, reason),
                        "trade": "flatten_reversal",
                        "match_id": mid,
                        "token_id": tid,
                    }
                )
                continue
            ek = f"flatten_retry|{mid}|{lib.now_cn_iso()}|{reason[:80]}"
            logger.error(
                "ALERT flatten_retry match=%s token=%s… attempt=%s reason=%s",
                mid,
                tid[:12],
                attempts,
                reason[:200],
            )
            out.append(
                self._flatten_lot(
                    lot,
                    event_key=ek,
                    reason=reason,
                    match_ev={
                        "match_id": mid,
                        "home": lot.get("home"),
                        "away": lot.get("away"),
                        "home_score": (lot.get("entry_score") or [None, None])[0]
                        if isinstance(lot.get("entry_score"), list)
                        else None,
                        "away_score": (lot.get("entry_score") or [None, None])[1]
                        if isinstance(lot.get("entry_score"), list)
                        else None,
                    },
                )
            )
        return out

    def _flatten_lot(
        self,
        lot: dict[str, Any],
        *,
        event_key: str,
        reason: str,
        match_ev: dict[str, Any],
    ) -> dict[str, Any]:
        token_id = str(lot.get("token_id") or "")
        mid = str(lot.get("match_id") or "")
        # Success-only idempotency: failures stay retryable (new retry keys / pending).
        key = f"{event_key}|{token_id}|flatten_reversal"
        if key in self._flatten_done:
            return {
                "idempotency_key": key,
                "status": "skipped",
                "skip_reason": "already_flattened",
                "trade": "flatten_reversal",
            }

        planned_shares = float(lot.get("shares") or 0)
        meta = {
            "match_id": mid,
            "home": lot.get("home") or match_ev.get("home") or "",
            "away": lot.get("away") or match_ev.get("away") or "",
            "home_score": match_ev.get("home_score"),
            "away_score": match_ev.get("away_score"),
        }
        quote_stub = {
            "market_key": lot.get("market_key"),
            "family": lot.get("family"),
            "token_id": token_id,
            "trade": "flatten_reversal",
            "settlement": "REVERSAL",
            "tick_size": lot.get("tick_size") or "0.01",
            "neg_risk": lot.get("neg_risk"),
        }

        # Dry-run lots: log intent and close ledger (never post CLOB for simulated buys).
        lot_live = bool(lot.get("live"))
        if not lot_live:
            plan = FillPlan(
                trade="flatten_reversal",
                side="SELL",
                take_depth="emergency",
                order_type="FAK",
                shares=planned_shares,
                usdc=0.0,
                worst_price=0.0,
                levels_used=0,
                levels=[],
                skip_reason=None,
            )
            row = self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=plan,
                status="flatten_dry_run",
                skip_reason=reason,
                response=None,
                success=True,
                idempotency_key=key,
                live=False,
            )
            self._flatten_done.add(key)
            self.ledger.mark_closed(token_id, mid, reason=reason)
            logger.info(
                "flatten dry-run match=%s token=%s… shares=%.4f (%s)",
                mid,
                token_id[:12],
                planned_shares,
                reason,
            )
            return row

        # Live FAK sell: cancel locks, haircut size, min_price=0.2 (dump fallback).
        try:
            trader = self.ensure_trader()
            if trader is None:
                raise RuntimeError("no trader for flatten")

            try:
                trader.refresh_conditional_allowance(token_id)
            except Exception:  # noqa: BLE001
                pass
            try:
                trader.cancel_orders_for_asset(token_id)
            except Exception:  # noqa: BLE001
                pass

            bal = Decimal(str(trader.get_conditional_balance(token_id)))
            if bal > 0 and lot.get("fill_status") == FILL_STATUS_PENDING:
                self.ledger.mark_fill_open(token_id, mid)
            shares = flatten_sell_shares(bal)
            if bal <= 0 or shares < FLATTEN_MIN_SHARES:
                # True empty → close. Tiny dust with no locks → close.
                # If balance still tradeable but sizing floored to 0, or we only
                # see dust while locks may remain, keep pending (Bodø case).
                if bal <= 0:
                    if lot.get("fill_status") == FILL_STATUS_PENDING:
                        attempts = int(lot.get("flatten_attempts") or 0)
                        if attempts >= FLATTEN_DELAYED_FILL_MAX_ATTEMPTS:
                            close_r = flatten_reason_append(
                                reason, "delayed_fill_never_appeared", f"attempts={attempts}"
                            )
                            row = self._record(
                                quote_stub,
                                event_key=event_key,
                                match_meta=meta,
                                plan=None,
                                status="flatten_abandoned",
                                skip_reason="delayed_fill_never_appeared",
                                response=None,
                                success=False,
                                idempotency_key=key,
                                live=True,
                            )
                            self._flatten_done.add(key)
                            self.ledger.mark_closed(token_id, mid, reason=close_r)
                            self._buy_blocked_matches.discard(mid)
                            alert = (
                                f"ALERT flatten_give_up match={mid} token={token_id[:12]}… "
                                f"delayed fill never appeared after {attempts} attempts"
                            )
                            logger.error(alert)
                            print(alert, flush=True)
                            return row
                        # Delayed fill not yet visible — keep pending; don't zombie-close.
                        self.ledger.mark_pending_flatten(
                            token_id,
                            mid,
                            reason=flatten_reason_append(reason, "awaiting_delayed_fill"),
                        )
                        self._buy_blocked_matches.add(mid)
                        row = self._record(
                            quote_stub,
                            event_key=event_key,
                            match_meta=meta,
                            plan=None,
                            status="flatten_skipped",
                            skip_reason="awaiting_delayed_fill",
                            response=None,
                            success=False,
                            idempotency_key=key,
                            live=True,
                        )
                        return row
                    row = self._record(
                        quote_stub,
                        event_key=event_key,
                        match_meta=meta,
                        plan=None,
                        status="flatten_skipped",
                        skip_reason="no_position_on_flatten",
                        response=None,
                        success=False,
                        idempotency_key=key,
                        live=True,
                    )
                    self._flatten_done.add(key)
                    self.ledger.mark_closed(token_id, mid, reason=reason + "|empty")
                    self._buy_blocked_matches.discard(mid)
                    return row
                if bal >= FLATTEN_MIN_SHARES:
                    # Have inventory but cannot size a legal sell this tick.
                    self.ledger.mark_pending_flatten(
                        token_id,
                        mid,
                        reason=flatten_reason_append(
                            reason, f"unsellable_bal={bal:.6f}", f"floor={shares}"
                        ),
                    )
                    alert = (
                        f"ALERT flatten_unsellable match={mid} token={token_id[:12]}… "
                        f"bal={bal} sell={shares} — keep pending"
                    )
                    logger.warning(alert)
                    print(alert, flush=True)
                    return self._record(
                        quote_stub,
                        event_key=event_key,
                        match_meta=meta,
                        plan=None,
                        status="flatten_error",
                        skip_reason=f"unsellable_bal={bal}",
                        response=None,
                        success=False,
                        idempotency_key=key,
                        live=True,
                    )
                dust_reason = f"{reason}|dust_bal={bal:.6f}|floor={shares}"
                row = self._record(
                    quote_stub,
                    event_key=event_key,
                    match_meta=meta,
                    plan=None,
                    status="flatten_skipped",
                    skip_reason="flatten_dust",
                    response=None,
                    success=True,
                    idempotency_key=key,
                    live=True,
                )
                self._flatten_done.add(key)
                self.ledger.mark_closed(token_id, mid, reason=dust_reason)
                alert = (
                    f"ALERT flatten_dust match={mid} token={token_id[:12]}… "
                    f"bal={bal} sell={shares} < {FLATTEN_MIN_SHARES} — closed"
                )
                logger.warning(alert)
                print(alert, flush=True)
                return row

            tick = str(lot.get("tick_size") or "0.01") or "0.01"
            neg = lot.get("neg_risk")
            neg_risk = bool(neg) if neg is not None else None
            min_px = FLATTEN_MIN_PRICE
            response, shares, ok = self._flatten_post_sell(
                trader,
                token_id=token_id,
                shares=shares,
                tick=tick,
                min_price=min_px,
                neg_risk=neg_risk,
            )

            residual = Decimal("-1")
            try:
                residual = Decimal(str(trader.get_conditional_balance(token_id)))
            except Exception:  # noqa: BLE001
                residual = Decimal("-1")

            # No fill at 0.2 (thin book): one dump attempt at 0.01.
            if (
                residual >= FLATTEN_MIN_SHARES
                and bal > 0
                and residual >= bal - Decimal("0.02")
            ):
                try:
                    trader.cancel_orders_for_asset(token_id)
                except Exception:  # noqa: BLE001
                    pass
                dump_shares = flatten_sell_shares(residual)
                if dump_shares >= FLATTEN_MIN_SHARES:
                    logger.warning(
                        "flatten dump retry match=%s token=%s… shares=%s min=%s",
                        mid,
                        token_id[:12],
                        dump_shares,
                        FLATTEN_DUMP_MIN_PRICE,
                    )
                    response, shares, ok = self._flatten_post_sell(
                        trader,
                        token_id=token_id,
                        shares=dump_shares,
                        tick=tick,
                        min_price=FLATTEN_DUMP_MIN_PRICE,
                        neg_risk=neg_risk,
                    )
                    min_px = FLATTEN_DUMP_MIN_PRICE
                    try:
                        residual = Decimal(
                            str(trader.get_conditional_balance(token_id))
                        )
                    except Exception:  # noqa: BLE001
                        residual = Decimal("-1")

            residual_floor = (
                floor_shares(residual) if residual >= 0 else Decimal("-1")
            )
            fully_flat = ok and residual >= 0 and residual_floor < FLATTEN_MIN_SHARES

            plan = FillPlan(
                trade="flatten_reversal",
                side="SELL",
                take_depth="emergency",
                order_type="FAK",
                shares=float(shares),
                usdc=0.0,
                worst_price=float(min_px),
                levels_used=0,
                levels=[],
                skip_reason=None,
            )
            row = self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=plan,
                status="flatten_posted",
                skip_reason=reason
                + (
                    f"|residual={float(residual):.4f}"
                    if residual > FLATTEN_MIN_SHARES
                    else ""
                ),
                response=response,
                success=fully_flat,
                idempotency_key=key,
                live=True,
            )
            if fully_flat:
                self._flatten_done.add(key)
                close_r = reason
                if residual > 0:
                    close_r += f"|dust_residual={residual}"
                self.ledger.mark_closed(token_id, mid, reason=close_r)
            else:
                alert = (
                    f"ALERT flatten_incomplete match={mid} token={token_id[:12]}… "
                    f"sold≈{shares} residual={residual} ok={ok} — will retry"
                )
                logger.error(alert)
                print(alert, flush=True)
                self.ledger.mark_pending_flatten(
                    token_id,
                    mid,
                    reason=flatten_reason_append(
                        reason,
                        f"incomplete residual={float(residual):.4f}"
                        if residual >= 0
                        else "incomplete",
                    ),
                )
            return row
        except Exception as e:  # noqa: BLE001
            if is_not_enough_balance_error(e):
                recovered = self._flatten_retry_after_balance_gate(
                    lot,
                    event_key=event_key,
                    reason=reason,
                    match_ev=match_ev,
                    key=key,
                    quote_stub=quote_stub,
                    meta=meta,
                    err=e,
                )
                if recovered is not None:
                    return recovered
            terminal = is_terminal_flatten_error(e)
            alert = (
                f"ALERT flatten_failed match={mid} token={token_id[:12]}… "
                f"err={e} — "
                + ("stopped retry (terminal)" if terminal else "will retry")
            )
            logger.exception(alert)
            print(alert, flush=True)
            if terminal:
                self._flatten_done.add(key)
                self.ledger.mark_closed(
                    token_id,
                    mid,
                    reason=flatten_reason_append(reason, "terminal", str(e)),
                )
            else:
                self.ledger.mark_pending_flatten(
                    token_id,
                    mid,
                    reason=flatten_reason_append(reason, f"err={e}"),
                )
            return self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=None,
                status="flatten_error" if not terminal else "flatten_abandoned",
                skip_reason=str(e),
                response=None,
                success=False,
                idempotency_key=key,
                live=True,
            )

    def _flatten_post_sell(
        self,
        trader: ClobTrader,
        *,
        token_id: str,
        shares: Decimal,
        tick: str,
        min_price: Decimal,
        neg_risk: bool | None,
    ) -> tuple[dict[str, Any], Decimal, bool]:
        response = trader.post_market_sell(
            token_id,
            shares,
            tick,
            min_price=min_price,
            order_type="FAK",
            neg_risk=neg_risk,
        )
        return response, shares, trader.is_order_success(response)

    def _flatten_retry_after_balance_gate(
        self,
        lot: dict[str, Any],
        *,
        event_key: str,
        reason: str,
        match_ev: dict[str, Any],
        key: str,
        quote_stub: dict[str, Any],
        meta: dict[str, Any],
        err: Exception,
    ) -> dict[str, Any] | None:
        """Cancel + sell free/haircut size after CLOB matched-order gate rejects.

        No inline sleep (keeps watch responsive). If still locked, leave
        ``pending_flatten`` for the next tick's ``retry_pending_flattens``.
        """
        token_id = str(lot.get("token_id") or "")
        mid = str(lot.get("match_id") or "")
        trader = self.ensure_trader()
        if trader is None:
            return None
        gate = parse_balance_gate_error(err)
        logger.warning(
            "flatten balance-gate match=%s token=%s… gate=%s — cancel+resize",
            mid,
            token_id[:12],
            {k: str(v) for k, v in (gate or {}).items()},
        )
        try:
            trader.cancel_orders_for_asset(token_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            trader.refresh_conditional_allowance(token_id)
        except Exception:  # noqa: BLE001
            pass

        try:
            bal = Decimal(str(trader.get_conditional_balance(token_id)))
        except Exception:  # noqa: BLE001
            bal = Decimal("0")

        # Trust gate free only while live bal still looks like the same bag.
        free = gate_free_cap(gate, bal)
        shares = flatten_sell_shares_available(bal, free=free)
        if shares < FLATTEN_MIN_SHARES and free is not None and free >= FLATTEN_MIN_SHARES:
            shares = floor_shares(free)
        if shares < FLATTEN_MIN_SHARES:
            # Locked inventory or true dust. Never dust-close while matched
            # still holds a tradeable bag — next tick retries after cancel.
            if gate_has_locked_inventory(gate) or bal >= FLATTEN_MIN_SHARES:
                lock_reason = (
                    f"{reason}|err={err}|await_unlock|matched="
                    f"{(gate or {}).get('matched')}|free={free}|bal={bal}"
                )
                self.ledger.mark_pending_flatten(token_id, mid, reason=lock_reason[:300])
                alert = (
                    f"ALERT flatten_locked match={mid} token={token_id[:12]}… "
                    f"bal={bal} free={free} matched={(gate or {}).get('matched')} "
                    f"— pending next tick"
                )
                logger.warning(alert)
                print(alert, flush=True)
                return self._record(
                    quote_stub,
                    event_key=event_key,
                    match_meta=meta,
                    plan=None,
                    status="flatten_error",
                    skip_reason=f"{err}|await_unlock",
                    response=None,
                    success=False,
                    idempotency_key=key,
                    live=True,
                )
            self.ledger.mark_pending_flatten(
                token_id,
                mid,
                reason=flatten_reason_append(
                    reason, f"err={err}", f"free<{FLATTEN_MIN_SHARES}"
                ),
            )
            return self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=None,
                status="flatten_error",
                skip_reason=str(err),
                response=None,
                success=False,
                idempotency_key=key,
                live=True,
            )

        tick = str(lot.get("tick_size") or "0.01") or "0.01"
        neg = lot.get("neg_risk")
        neg_risk = bool(neg) if neg is not None else None
        try:
            response, shares, ok = self._flatten_post_sell(
                trader,
                token_id=token_id,
                shares=shares,
                tick=tick,
                min_price=FLATTEN_DUMP_MIN_PRICE,
                neg_risk=neg_risk,
            )
        except Exception as e2:  # noqa: BLE001
            self.ledger.mark_pending_flatten(
                token_id,
                mid,
                reason=f"{reason}|err={err}|retry_err={e2}"[:300],
            )
            return self._record(
                quote_stub,
                event_key=event_key,
                match_meta=meta,
                plan=None,
                status="flatten_error",
                skip_reason=f"{err}|retry={e2}",
                response=None,
                success=False,
                idempotency_key=key,
                live=True,
            )

        try:
            residual = Decimal(str(trader.get_conditional_balance(token_id)))
        except Exception:  # noqa: BLE001
            residual = Decimal("-1")
        residual_floor = floor_shares(residual) if residual >= 0 else Decimal("-1")
        fully_flat = ok and residual >= 0 and residual_floor < FLATTEN_MIN_SHARES
        plan = FillPlan(
            trade="flatten_reversal",
            side="SELL",
            take_depth="emergency",
            order_type="FAK",
            shares=float(shares),
            usdc=0.0,
            worst_price=float(FLATTEN_DUMP_MIN_PRICE),
            levels_used=0,
            levels=[],
            skip_reason=None,
        )
        row = self._record(
            quote_stub,
            event_key=event_key,
            match_meta=meta,
            plan=plan,
            status="flatten_posted",
            skip_reason=(
                reason + f"|balance_gate_retry|residual={float(residual):.4f}"
                if residual > FLATTEN_MIN_SHARES
                else reason + "|balance_gate_retry"
            ),
            response=response,
            success=fully_flat,
            idempotency_key=key,
            live=True,
        )
        if fully_flat:
            self._flatten_done.add(key)
            self.ledger.mark_closed(
                token_id,
                mid,
                reason=flatten_reason_append(reason, "balance_gate_retry"),
            )
        else:
            self.ledger.mark_pending_flatten(
                token_id,
                mid,
                reason=flatten_reason_append(reason, "balance_gate_partial"),
            )
        return row

    def _position_shares(self, token_id: str) -> float | None:
        """Available conditional balance.

        Plan: sell_lose must check position; skip if insufficient.
        Returns None when the balance API cannot be queried (caller should skip).
        """
        try:
            trader = self.ensure_trader()
            if trader is None:
                return None
            bal = trader.get_conditional_balance(token_id)
            return float(bal)
        except Exception as e:  # noqa: BLE001
            logger.warning("position query failed token=%s: %s", token_id[:12], e)
            return None

    def _record(
        self,
        quote: dict[str, Any],
        *,
        event_key: str,
        match_meta: dict[str, Any] | None,
        plan: FillPlan | None,
        status: str,
        skip_reason: str | None,
        response: dict | None,
        success: bool,
        idempotency_key: str,
        live: bool | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta = match_meta or {}
        row: dict[str, Any] = {
            "quoted_at": lib.now_cn_iso(),
            "status": status,
            "success": success,
            "live": bool(live) if live is not None else bool(self.settings.live),
            "idempotency_key": idempotency_key,
            "event_key": event_key,
            "match_id": meta.get("match_id") or quote.get("match_id") or "",
            "home": meta.get("home") or "",
            "away": meta.get("away") or "",
            "home_score": meta.get("home_score"),
            "away_score": meta.get("away_score"),
            "event_type": meta.get("event_type")
            or signal_from_event_key(event_key)
            or "",
            "market_key": quote.get("market_key"),
            "family": quote.get("family"),
            "outcome": quote.get("outcome"),
            "settlement": quote.get("settlement"),
            "token_id": quote.get("token_id"),
            "trade": quote.get("trade"),
            "net_edge": quote.get("net_edge"),
            "gross_edge": quote.get("gross_edge"),
            "fee": quote.get("fee"),
            "best_bid": quote.get("best_bid"),
            "best_ask": quote.get("best_ask"),
            "take_depth": self.settings.take_depth,
            "plan": plan.to_dict() if plan else None,
            "skip_reason": skip_reason,
            "response": response,
        }
        if extra:
            row.update(extra)
        lib.append_jsonl_async(self.trades_path, [row])
        return row

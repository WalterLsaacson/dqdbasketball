"""GTC/GTD rest-buy ladder after FAK (A/B remaining toward target)."""

from __future__ import annotations

import os
from decimal import ROUND_DOWN, Decimal
from typing import Any

# Willing-to-pay basketball entry bids after FAK.
DEFAULT_REST_PRICES = (0.99,)
# Locally settled WIN asks through this price use FAK; higher asks use rest.
FAK_ZONE_MAX_ASK = 0.995
# When the book bid already sits at 0.99, keep the single-price ladder.
REST_CONCENTRATE_BID = 0.99
# 0 → GTC (stay until reversal / FT / manual cancel). >0 → GTD seconds.
DEFAULT_REST_EXPIRE_S = 0.0
MIN_REST_USDC = 1.0
# Soccer CLOB limit bids reject below 5 shares (~$4.975 @ 0.995). Pitch-gate
# rest therefore spends ``QUOTE_REST_USDC`` (default $5), not ``QUOTE_MAX_USDC``.
DEFAULT_REST_USDC = 5.0
MIN_REST_SHARES = 5.0
# Leave this much collateral unused when the target does not fit. CLOB
# checks notional plus fee, so a bid sized to the full balance is rejected
# by about two cents. $2 covers that buffer (55 → 53).
WALLET_ORDER_HAIRCUT_USDC = 2.0
_SHARE_Q = Decimal("0.01")
# Basketball CLOB books use a 0.001 tick. Rest posts directly at 0.99.
REST_LIMIT_MIN_TICK = 0.001
REST_FALLBACK_TICK = 0.001
# Master switch for GTD/GTC rest bids (pitch-gate fallback + cushion). Off until stable.
DEFAULT_REST_ENABLED = True


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def rest_enabled() -> bool:
    """``QUOTE_REST_ENABLED`` — enable GTC rest bids (on by default)."""
    return _env_bool("QUOTE_REST_ENABLED", DEFAULT_REST_ENABLED)


def rest_target_usdc() -> float:
    """Pitch-gate rest notional. Default $5 so 0.995 bids clear the 5-share floor."""
    raw = os.getenv("QUOTE_REST_USDC")
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_REST_USDC)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_REST_USDC)


def clip_rest_to_balance(target: float, available: float | None) -> float:
    """Configured size, or wallet USDC minus $2 when the wallet is smaller.

    ``available is None`` (dry / lookup miss) keeps ``target``. A target that
    already fits under ``available - 2`` is unchanged. Example: target 300
    and wallet 55 places 53, so the fee-inclusive order still clears.
    """
    try:
        want = max(0.0, float(target or 0))
    except (TypeError, ValueError):
        want = 0.0
    if available is None:
        return want
    try:
        have = max(0.0, float(available))
    except (TypeError, ValueError):
        return want
    room = max(0.0, have - WALLET_ORDER_HAIRCUT_USDC)
    return min(want, room)


def rest_place_usdc_for_wallet(
    place_usdc: float,
    *,
    available: float | None,
    working: float = 0.0,
    replace: bool = False,
) -> float:
    """Clip a new rest bid to spendable USDC.

    Replace cancels the working bid first, so that locked USDC is counted
    as available again. Add-only uses current wallet cash.
    """
    room = available
    if available is not None and replace:
        try:
            room = max(0.0, float(available)) + max(0.0, float(working or 0))
        except (TypeError, ValueError):
            room = available
    return clip_rest_to_balance(place_usdc, room)


def rest_min_shares(quote: dict[str, Any] | None) -> float:
    """CLOB share floor from the book; default ``MIN_REST_SHARES`` if unpublished."""
    raw = (quote or {}).get("min_order_size")
    if raw is None or str(raw).strip() == "":
        return float(MIN_REST_SHARES)
    try:
        shares = float(raw)
    except (TypeError, ValueError):
        return float(MIN_REST_SHARES)
    return shares if shares > 0 else float(MIN_REST_SHARES)


def min_rest_usdc(price: float, *, min_shares: float = MIN_REST_SHARES) -> float:
    """USDC needed for ``min_shares`` at ``price`` (CLOB limit floor)."""
    try:
        px = float(price)
    except (TypeError, ValueError):
        return float(MIN_REST_USDC)
    if px <= 0:
        return float(MIN_REST_USDC)
    return round(max(float(min_shares), 0.0) * px, 6)


def format_rest_tick(tick: float) -> str:
    if abs(tick - round(tick, 2)) < 1e-9:
        return f"{tick:.2f}"
    if abs(tick - round(tick, 3)) < 1e-9:
        return f"{tick:.3f}"
    return str(tick)


def rest_limit_tick_size(tick_size: str | float | None) -> str:
    """Tick string for GTD/GTC limit posts.

    Do not trust published 0.001 metadata — CLOB's soccer minimum is 0.01.
    Finer ticks are clamped up; 0.995 then snaps to 0.99.
    """
    try:
        tick = float(tick_size or REST_FALLBACK_TICK)
    except (TypeError, ValueError):
        tick = REST_FALLBACK_TICK
    tick = max(REST_LIMIT_MIN_TICK, tick)
    return format_rest_tick(tick)


def rest_expire_s() -> float:
    """Seconds until GTD expiry. ``0`` (default) → GTC, no clock expiry."""
    raw = os.getenv("QUOTE_REST_EXPIRE_S")
    if raw is None or str(raw).strip() == "":
        return float(DEFAULT_REST_EXPIRE_S)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(DEFAULT_REST_EXPIRE_S)


def ask_in_fak_zone(best_ask: Any) -> bool:
    """True when a tradeable ask exists — remainder should FAK, not rest."""
    if best_ask is None:
        return False
    try:
        return float(best_ask) <= FAK_ZONE_MAX_ASK + 1e-12
    except (TypeError, ValueError):
        return False


def select_rest_prices(
    *,
    best_bid: Any = None,
    best_ask: Any = None,
    prices: tuple[float, ...] | None = None,
) -> tuple[float, ...]:
    """Pick rest ladder prices; empty tuple means do not rest (FAK zone)."""
    if prices is not None:
        return prices
    if ask_in_fak_zone(best_ask):
        return ()
    try:
        bid = float(best_bid) if best_bid is not None else None
    except (TypeError, ValueError):
        bid = None
    if bid is not None and bid + 1e-12 >= REST_CONCENTRATE_BID:
        return (REST_CONCENTRATE_BID,)
    return rest_prices()


def rest_prices() -> tuple[float, ...]:
    raw = os.getenv("QUOTE_REST_PRICES")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_REST_PRICES
    out: list[float] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            px = float(part)
        except (TypeError, ValueError):
            continue
        if 0.01 <= px < 1.0:
            out.append(px)
    return tuple(out) if out else DEFAULT_REST_PRICES


def snap_rest_price(price: float, tick_size: str | float | None) -> float | None:
    """Tick-snap a rest bid **down** (never pay above the target)."""
    try:
        px = float(price)
        tick = float(tick_size or REST_FALLBACK_TICK)
    except (TypeError, ValueError):
        return None
    if tick <= 0 or px <= 0:
        return None
    steps = (Decimal(str(px)) / Decimal(str(tick))).to_integral_value(
        rounding=ROUND_DOWN
    )
    snapped = float((steps * Decimal(str(tick))).quantize(Decimal("0.000001")))
    if snapped <= 0 or snapped >= 1.0 - 1e-12:
        return None
    return snapped


def resolve_rest_price(
    price: float, tick_size: str | float | None
) -> tuple[float, str] | None:
    """Snap ``price`` onto the book's CLOB-legal tick (no invented finer grid)."""
    tick = rest_limit_tick_size(tick_size)
    snapped = snap_rest_price(price, tick)
    if snapped is None:
        return None
    return snapped, tick


def shares_for_usdc(usdc: float, price: float) -> float:
    if price <= 0 or usdc <= 0:
        return 0.0
    sh = (Decimal(str(usdc)) / Decimal(str(price))).quantize(
        _SHARE_Q, rounding=ROUND_DOWN
    )
    return float(sh)


def allocate_rest_ladder(
    remaining_usdc: float,
    *,
    prices: tuple[float, ...] | None = None,
    tick_size: str | float | None = "0.01",
    floor_usdc: float = MIN_REST_USDC,
    best_bid: Any = None,
    best_ask: Any = None,
    min_shares: float = MIN_REST_SHARES,
) -> list[dict[str, Any]]:
    """Split remaining USDC across the configured basketball rest prices."""
    need = max(0.0, float(remaining_usdc))
    floor = max(0.0, float(floor_usdc))
    share_floor = max(0.0, float(min_shares))
    if need + 1e-12 < floor:
        return []
    chosen = select_rest_prices(best_bid=best_bid, best_ask=best_ask, prices=prices)
    if not chosen:
        return []
    snapped: list[tuple[float, str]] = []
    seen: set[float] = set()
    for raw in chosen:
        resolved = resolve_rest_price(raw, tick_size)
        if resolved is None:
            continue
        px, tick = resolved
        if px in seen:
            continue
        seen.add(px)
        snapped.append((px, tick))
    if not snapped:
        return []
    # Do not raise spend above the caller budget (QUOTE_MAX_USDC).
    level_floor = floor
    if share_floor > 0:
        lifted = min_rest_usdc(min(p for p, _ in snapped), min_shares=share_floor)
        if lifted <= need + 1e-12:
            need = max(need, lifted)
            level_floor = max(floor, lifted)
    # Only split when each side can still clear the share floor; otherwise
    # concentrate on the first price (avoids 2× min-share overspend).
    if len(snapped) == 1 or need + 1e-12 < 2.0 * level_floor:
        chunks = [need]
        snapped = snapped[:1]
    else:
        half = round(need / 2.0, 6)
        if half + 1e-12 < level_floor:
            chunks = [need]
            snapped = snapped[:1]
        else:
            chunks = [need - half, half]
            # First price (0.99) gets the larger leftover.
            if chunks[0] + 1e-12 < level_floor:
                chunks = [need]
                snapped = snapped[:1]
    levels: list[dict[str, Any]] = []
    share_tick = float(_SHARE_Q)
    for (px, tick), usdc in zip(snapped, chunks):
        if usdc + 1e-12 < floor:
            continue
        shares = shares_for_usdc(usdc, px)
        cost = round(shares * px, 6)
        need_floor = min(floor, usdc)
        # ROUND_DOWN can leave $1 → 0.9999; bump one share tick so the
        # notional still clears the floor (slight overspend ≤ one tick).
        if shares > 0 and cost + 1e-12 < need_floor:
            bumped = round(shares + share_tick, 6)
            bumped_cost = round(bumped * px, 6)
            if (
                bumped_cost + 1e-12 >= need_floor
                and bumped_cost <= usdc + share_tick * px + 1e-9
            ):
                shares, cost = bumped, bumped_cost
        if share_floor > 0 and shares + 1e-12 < share_floor:
            lifted_cost = round(float(share_floor) * px, 6)
            if lifted_cost <= usdc + 1e-9:
                shares = float(share_floor)
                cost = lifted_cost
            else:
                continue
        if shares <= 0 or cost + 1e-12 < need_floor:
            continue
        levels.append(
            {
                "price": px,
                "shares": round(shares, 6),
                "usdc": cost,
                "tick_size": tick,
            }
        )
    return levels

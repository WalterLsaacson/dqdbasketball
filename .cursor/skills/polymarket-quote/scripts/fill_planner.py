"""Plan fill size / worst price from bids_top / asks_top (top vs walk)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class FillPlan:
    """Executable market-order plan derived from local book depth."""

    trade: str  # buy_win | sell_lose
    side: str  # BUY | SELL
    take_depth: str  # top | walk
    order_type: str  # FOK | FAK
    shares: float
    usdc: float
    worst_price: float  # max_price for BUY, min_price for SELL
    levels_used: int
    levels: list[dict[str, Any]]
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(x: Any) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _levels_from_top(
    top: list[dict[str, Any]] | None,
    *,
    best_price: float | None,
    best_size: float | None,
) -> list[tuple[float, float]]:
    """Normalize to [(price, size), ...] ascending for asks / descending for bids."""
    out: list[tuple[float, float]] = []
    for lvl in top or []:
        p, s = _f(lvl.get("price")), _f(lvl.get("size"))
        if p is None or s is None or s <= 0:
            continue
        out.append((p, s))
    if not out and best_price is not None and best_size is not None and best_size > 0:
        out.append((best_price, best_size))
    return out


def plan_buy_win(
    quote: dict[str, Any],
    *,
    take_depth: str = "top",
    max_levels: int = 5,
    max_usdc: float = 5.0,
    max_shares: float = 25.0,
    max_slippage: float = 0.03,
    min_order_shares: float = 0.0,
) -> FillPlan:
    """Buy WIN token into asks."""
    asks = _levels_from_top(
        quote.get("asks_top"),
        best_price=_f(quote.get("best_ask")),
        best_size=_f(quote.get("best_ask_size")),
    )
    # asks_top should already be low→high; sort to be safe
    asks.sort(key=lambda x: x[0])

    empty = FillPlan(
        trade="buy_win",
        side="BUY",
        take_depth=take_depth,
        order_type="FAK",
        shares=0.0,
        usdc=0.0,
        worst_price=0.0,
        levels_used=0,
        levels=[],
        skip_reason="no_ask",
    )
    if not asks:
        return empty

    best = asks[0][0]
    cap_price = best + float(max_slippage) if take_depth == "walk" else best
    limit_levels = 1 if take_depth == "top" else max(1, int(max_levels))

    taken: list[dict[str, Any]] = []
    shares = 0.0
    usdc = 0.0
    worst = best

    for i, (price, size) in enumerate(asks):
        if i >= limit_levels:
            break
        if price > cap_price + 1e-12:
            break
        if shares >= float(max_shares) - 1e-12:
            break
        if usdc >= float(max_usdc) - 1e-12:
            break

        room_shares = float(max_shares) - shares
        room_usdc = float(max_usdc) - usdc
        affordable = room_usdc / price if price > 0 else 0.0
        take = min(size, room_shares, affordable)
        if take <= 1e-12:
            break
        cost = take * price
        taken.append({"price": price, "size": round(take, 6), "level_size": size})
        shares += take
        usdc += cost
        worst = price

    if shares <= 1e-12 or usdc <= 1e-12:
        empty.skip_reason = "zero_fill"
        return empty
    if float(min_order_shares) > 0 and shares + 1e-12 < float(min_order_shares):
        empty.skip_reason = f"shares={shares:.4f} < min_order={min_order_shares}"
        empty.levels = taken
        empty.levels_used = len(taken)
        empty.worst_price = worst
        empty.shares = round(shares, 6)
        empty.usdc = round(usdc, 6)
        return empty

    return FillPlan(
        trade="buy_win",
        side="BUY",
        take_depth=take_depth,
        order_type="FAK",
        shares=round(shares, 6),
        usdc=round(usdc, 6),
        worst_price=worst,
        levels_used=len(taken),
        levels=taken,
        skip_reason=None,
    )


def plan_sell_lose(
    quote: dict[str, Any],
    *,
    available_shares: float | None,
    take_depth: str = "top",
    max_levels: int = 5,
    max_shares: float = 25.0,
    max_slippage: float = 0.03,
    min_order_shares: float = 0.0,
) -> FillPlan:
    """Sell LOSE token into bids."""
    bids = _levels_from_top(
        quote.get("bids_top"),
        best_price=_f(quote.get("best_bid")),
        best_size=_f(quote.get("best_bid_size")),
    )
    # bids: high→low
    bids.sort(key=lambda x: x[0], reverse=True)

    empty = FillPlan(
        trade="sell_lose",
        side="SELL",
        take_depth=take_depth,
        # Sells and buys both use FAK (partial fill OK).
        order_type="FAK",
        shares=0.0,
        usdc=0.0,
        worst_price=0.0,
        levels_used=0,
        levels=[],
        skip_reason="no_bid",
    )
    if not bids:
        return empty

    if available_shares is not None and available_shares <= 0:
        empty.skip_reason = "no_position"
        return empty

    best = bids[0][0]
    floor_price = best - float(max_slippage) if take_depth == "walk" else best
    limit_levels = 1 if take_depth == "top" else max(1, int(max_levels))
    pos_cap = float(available_shares) if available_shares is not None else float(max_shares)

    taken: list[dict[str, Any]] = []
    shares = 0.0
    usdc = 0.0
    worst = best

    for i, (price, size) in enumerate(bids):
        if i >= limit_levels:
            break
        if price + 1e-12 < floor_price:
            break
        if shares >= float(max_shares) - 1e-12:
            break
        if shares >= pos_cap - 1e-12:
            break

        room = min(float(max_shares) - shares, pos_cap - shares, size)
        if room <= 1e-12:
            break
        taken.append({"price": price, "size": round(room, 6), "level_size": size})
        shares += room
        usdc += room * price
        worst = price

    if shares <= 1e-12:
        empty.skip_reason = "zero_fill"
        return empty
    if float(min_order_shares) > 0 and shares + 1e-12 < float(min_order_shares):
        empty.skip_reason = f"shares={shares:.4f} < min_order={min_order_shares}"
        empty.levels = taken
        empty.levels_used = len(taken)
        empty.worst_price = worst
        empty.shares = round(shares, 6)
        empty.usdc = round(usdc, 6)
        return empty

    return FillPlan(
        trade="sell_lose",
        side="SELL",
        take_depth=take_depth,
        order_type="FAK",
        shares=round(shares, 6),
        usdc=round(usdc, 6),
        worst_price=worst,
        levels_used=len(taken),
        levels=taken,
        skip_reason=None,
    )


def plan_fill(
    quote: dict[str, Any],
    *,
    take_depth: str = "top",
    max_levels: int = 5,
    max_usdc: float = 5.0,
    max_shares: float = 25.0,
    max_slippage: float = 0.03,
    min_order_shares: float = 0.0,
    available_shares: float | None = None,
) -> FillPlan:
    trade = str(quote.get("trade") or "")
    if trade == "buy_win":
        return plan_buy_win(
            quote,
            take_depth=take_depth,
            max_levels=max_levels,
            max_usdc=max_usdc,
            max_shares=max_shares,
            max_slippage=max_slippage,
            min_order_shares=min_order_shares,
        )
    if trade == "sell_lose":
        return plan_sell_lose(
            quote,
            available_shares=available_shares,
            take_depth=take_depth,
            max_levels=max_levels,
            max_shares=max_shares,
            max_slippage=max_slippage,
            min_order_shares=min_order_shares,
        )
    return FillPlan(
        trade=trade or "unknown",
        side="",
        take_depth=take_depth,
        order_type="",
        shares=0.0,
        usdc=0.0,
        worst_price=0.0,
        levels_used=0,
        levels=[],
        skip_reason=f"unknown_trade={trade!r}",
    )

"""Rest a 0.999 sell for shares bought near 0.99.

The sell stays on the book until someone lifts it. Already-listed shares are
not posted again. A later fill only adds a new sell for the extra shares, and
only once that extra clears the CLOB share minimum.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any

EXIT_SELL_PRICE = 0.999
EXIT_SELL_TICK = "0.001"
EXIT_MIN_ENTRY = 0.98
EXIT_MIN_SHARES = 5.0
_SHARE_Q = Decimal("0.01")


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def floor_shares(shares: float) -> float:
    if shares <= 0:
        return 0.0
    return float(Decimal(str(shares)).quantize(_SHARE_Q, rounding=ROUND_DOWN))


def note_exit_fill(row: dict[str, Any], added_shares: float, entry_price: float) -> None:
    """Count shares that should be offered at ``EXIT_SELL_PRICE``.

    The first note on a lot uses the average entry already stored (so a
    historical 0.99 lot is fully eligible). Later notes add only fills whose
    own price clears ``EXIT_MIN_ENTRY``.
    """
    added = max(0.0, _f(added_shares))
    if "exit_eligible_shares" in row:
        if _f(entry_price) + 1e-12 >= EXIT_MIN_ENTRY and added > 0:
            row["exit_eligible_shares"] = _f(row.get("exit_eligible_shares")) + added
        return
    shares = _f(row.get("shares"))
    usdc = _f(row.get("usdc"))
    avg = (usdc / shares) if shares > 1e-12 else 0.0
    if avg + 1e-12 >= EXIT_MIN_ENTRY:
        row["exit_eligible_shares"] = shares
    elif _f(entry_price) + 1e-12 >= EXIT_MIN_ENTRY:
        row["exit_eligible_shares"] = added
    else:
        row["exit_eligible_shares"] = 0.0


def ensure_exit_eligible(row: dict[str, Any]) -> None:
    """Backfill eligibility for lots bought before exit sells existed."""
    if "exit_eligible_shares" in row:
        return
    shares = _f(row.get("shares"))
    usdc = _f(row.get("usdc"))
    avg = (usdc / shares) if shares > 1e-12 else 0.0
    row["exit_eligible_shares"] = shares if avg + 1e-12 >= EXIT_MIN_ENTRY else 0.0


def is_dongqiudi_match_id(match_id: Any) -> bool:
    """Dongqiudi match ids are decimal. Polymarket condition ids are 0x…."""
    return str(match_id or "").isdigit()


def untracked_fill(order: dict[str, Any], known_order_ids: set[str]) -> tuple[float, float]:
    """Shares/USDC to book for one open bid that is not already on the lot."""
    oid = str(order.get("id") or order.get("order_id") or "")
    if not oid or oid in known_order_ids:
        return 0.0, 0.0
    filled = _f(order.get("size_matched") if "size_matched" in order else order.get("filled_shares"))
    px = _f(order.get("price"))
    if filled <= 0 or px <= 0:
        return 0.0, 0.0
    return filled, filled * px


def exit_unlisted_shares(
    row: dict[str, Any],
    *,
    min_shares: float = EXIT_MIN_SHARES,
) -> float:
    """Shares to post now. Zero when the gap is already listed or under the minimum."""
    gap = _f(row.get("exit_eligible_shares")) - _f(row.get("exit_listed_shares"))
    size = floor_shares(gap)
    floor = max(0.0, _f(min_shares))
    if size + 1e-9 < floor:
        return 0.0
    return size

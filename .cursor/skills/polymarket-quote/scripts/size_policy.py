"""Buy size from ask: binary high/low usdc, scale shares with usdc."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ask >= threshold → usdc; below all thresholds → hard max_usdc (default $20).
DEFAULT_SIZE_TIERS: tuple[tuple[float, float], ...] = ((0.98, 10.0),)


@dataclass(frozen=True)
class BuySizeCaps:
    max_usdc: float
    max_shares: float
    tier_usdc: float
    open_usdc: float
    remaining_open: float | None
    ask: float
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_usdc": self.max_usdc,
            "max_shares": self.max_shares,
            "tier_usdc": self.tier_usdc,
            "open_usdc": self.open_usdc,
            "remaining_open": self.remaining_open,
            "ask": self.ask,
            "skip_reason": self.skip_reason,
        }


def parse_size_tiers(raw: str | None) -> list[tuple[float, float]]:
    """Parse ``0.98:10`` → sorted (min_ask, usdc); ask >= threshold uses usdc."""
    if raw is None or not str(raw).strip():
        return list(DEFAULT_SIZE_TIERS)
    out: list[tuple[float, float]] = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"bad size tier {part!r}; expected ask:usdc")
        a_s, u_s = part.split(":", 1)
        ask = float(a_s.strip())
        usdc = float(u_s.strip())
        if ask <= 0 or usdc < 0:
            raise ValueError(f"bad size tier values {part!r}")
        out.append((ask, usdc))
    if not out:
        return list(DEFAULT_SIZE_TIERS)
    out.sort(key=lambda x: x[0])
    return out


def format_size_tiers(tiers: list[tuple[float, float]] | tuple[tuple[float, float], ...]) -> str:
    return ",".join(f"{a:g}:{u:g}" for a, u in tiers)


def tier_max_usdc(
    ask: float,
    tiers: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    *,
    fallback: float,
) -> float:
    """Highest threshold with ask >= threshold wins; else ``fallback`` (low-ask size)."""
    hard = float(fallback)
    if not tiers:
        return hard
    chosen: float | None = None
    for threshold, usdc in tiers:
        if float(ask) + 1e-12 >= float(threshold):
            chosen = float(usdc)
    if chosen is None:
        return hard
    return min(chosen, hard)


def scale_max_shares(
    *,
    max_shares: float,
    max_usdc_cap: float,
    eff_usdc: float,
    ask: float,
) -> float:
    """Keep shares in lockstep with usdc so neither cap alone overshoots.

    1) Scale baseline max_shares by eff_usdc / max_usdc_cap
    2) Also cap by eff_usdc / ask so shares*ask cannot exceed the usdc budget
    """
    cap = max(0.0, float(max_usdc_cap))
    eff = max(0.0, float(eff_usdc))
    shares_cap = max(0.0, float(max_shares))
    if cap <= 0 or eff <= 0 or shares_cap <= 0:
        return 0.0
    scaled = shares_cap * (eff / cap)
    px = max(float(ask), 1e-9)
    by_notional = eff / px
    return max(0.0, min(scaled, by_notional, shares_cap))


def compute_buy_size_caps(
    ask: float | None,
    *,
    max_usdc: float,
    max_shares: float,
    tiers: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    open_usdc: float = 0.0,
    max_open_usdc: float | None = None,
    floor_usdc: float = 1.0,
) -> BuySizeCaps:
    """Resolve effective buy caps for this ask + open exposure."""
    try:
        px = float(ask) if ask is not None else None
    except (TypeError, ValueError):
        px = None
    if px is None or px <= 0:
        return BuySizeCaps(
            max_usdc=0.0,
            max_shares=0.0,
            tier_usdc=0.0,
            open_usdc=float(open_usdc or 0.0),
            remaining_open=None,
            ask=float(ask or 0.0),
            skip_reason="size_policy_no_ask",
        )

    hard = max(0.0, float(max_usdc))
    tier = tier_max_usdc(px, tiers, fallback=hard)
    open_u = max(0.0, float(open_usdc or 0.0))
    remaining: float | None = None
    eff = min(tier, hard)
    if max_open_usdc is not None and float(max_open_usdc) > 0:
        remaining = max(0.0, float(max_open_usdc) - open_u)
        eff = min(eff, remaining)

    floor = max(0.0, float(floor_usdc))
    if eff + 1e-12 < floor:
        return BuySizeCaps(
            max_usdc=0.0,
            max_shares=0.0,
            tier_usdc=tier,
            open_usdc=open_u,
            remaining_open=remaining,
            ask=px,
            skip_reason=(
                "size_policy_open_budget"
                if remaining is not None and remaining + 1e-12 < floor
                else "size_policy_below_floor"
            ),
        )

    shares = scale_max_shares(
        max_shares=max_shares,
        max_usdc_cap=hard,
        eff_usdc=eff,
        ask=px,
    )
    if shares <= 0:
        return BuySizeCaps(
            max_usdc=0.0,
            max_shares=0.0,
            tier_usdc=tier,
            open_usdc=open_u,
            remaining_open=remaining,
            ask=px,
            skip_reason="size_policy_zero_shares",
        )

    return BuySizeCaps(
        max_usdc=eff,
        max_shares=shares,
        tier_usdc=tier,
        open_usdc=open_u,
        remaining_open=remaining,
        ask=px,
        skip_reason=None,
    )

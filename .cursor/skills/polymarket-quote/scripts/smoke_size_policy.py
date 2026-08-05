#!/usr/bin/env python3
"""Smoke: binary ask usdc (>=0.98→10, else 20) + open cap."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from size_policy import (  # noqa: E402
    DEFAULT_SIZE_TIERS,
    compute_buy_size_caps,
    parse_size_tiers,
    scale_max_shares,
    tier_max_usdc,
)

TIERS = parse_size_tiers("0.98:10")


def main() -> int:
    assert TIERS == [(0.98, 10.0)]
    assert list(DEFAULT_SIZE_TIERS) == [(0.98, 10.0)]

    assert tier_max_usdc(0.97, TIERS, fallback=20) == 20
    assert tier_max_usdc(0.979, TIERS, fallback=20) == 20
    assert tier_max_usdc(0.98, TIERS, fallback=20) == 10
    assert tier_max_usdc(0.99, TIERS, fallback=20) == 10
    assert tier_max_usdc(0.995, TIERS, fallback=20) == 10
    # hard cap still wins
    assert tier_max_usdc(0.97, TIERS, fallback=15) == 15
    assert tier_max_usdc(0.99, TIERS, fallback=8) == 8

    sh20 = scale_max_shares(max_shares=25, max_usdc_cap=20, eff_usdc=20, ask=0.97)
    assert abs(sh20 - min(25.0, 20 / 0.97)) < 1e-6

    caps_lo = compute_buy_size_caps(
        0.97,
        max_usdc=20,
        max_shares=25,
        tiers=TIERS,
        open_usdc=0,
        max_open_usdc=1000,
        floor_usdc=1,
    )
    assert caps_lo.skip_reason is None
    assert abs(caps_lo.max_usdc - 20) < 1e-9
    assert abs(caps_lo.max_shares - sh20) < 1e-6

    sh10 = scale_max_shares(max_shares=25, max_usdc_cap=20, eff_usdc=10, ask=0.98)
    caps_hi = compute_buy_size_caps(
        0.98,
        max_usdc=20,
        max_shares=25,
        tiers=TIERS,
        open_usdc=0,
        max_open_usdc=1000,
        floor_usdc=1,
    )
    assert caps_hi.skip_reason is None
    assert abs(caps_hi.max_usdc - 10) < 1e-9
    assert abs(caps_hi.max_shares - sh10) < 1e-6

    # open budget still clamps when remaining is small
    capped = compute_buy_size_caps(
        0.97,
        max_usdc=20,
        max_shares=25,
        tiers=TIERS,
        open_usdc=995,
        max_open_usdc=1000,
        floor_usdc=1,
    )
    assert abs(capped.max_usdc - 5) < 1e-9

    skip = compute_buy_size_caps(
        0.96,
        max_usdc=20,
        max_shares=25,
        tiers=TIERS,
        open_usdc=1000,
        max_open_usdc=1000,
        floor_usdc=1,
    )
    assert skip.skip_reason == "size_policy_open_budget"
    assert skip.max_usdc == 0 and skip.max_shares == 0

    print("ok: binary size (>=0.98→10 else 20) + open cap")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Smoke: flatten sell sizing + balance-gate error parse."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from trade_executor import (  # noqa: E402
    flatten_reason_append,
    flatten_sell_shares,
    flatten_sell_shares_available,
    floor_shares,
    gate_free_cap,
    gate_has_locked_inventory,
    is_not_enough_balance_error,
    is_terminal_flatten_error,
    parse_balance_gate_error,
)


def main() -> int:
    assert floor_shares(Decimal("3.333332")) == Decimal("3.33")
    assert flatten_sell_shares(Decimal("3.333332")) == Decimal("3.29")
    assert flatten_sell_shares(Decimal("1.0")) == Decimal("0.99")
    assert flatten_sell_shares(Decimal("0.015")) == Decimal("0.01")

    assert is_terminal_flatten_error("invalid maker amount")
    assert is_terminal_flatten_error(
        "PolyApiException[status_code=400, error_message={'error': 'invalid token id'}]"
    )
    assert not is_terminal_flatten_error("Request exception!")
    # Append should not explode on repeated delayed-fill tags.
    r = "ft_reversal_vs_entry ft=0-1"
    for _ in range(50):
        r = flatten_reason_append(r, "awaiting_delayed_fill")
    assert r.count("awaiting_delayed_fill") == 1
    assert len(r) < 200

    err = (
        "PolyApiException[status_code=400, error_message={'error': "
        "'not enough balance / allowance: the balance is not enough -> "
        "balance: 3333332, sum of matched orders: 3330000, "
        "order amount (inc. fees): 3330000'}]"
    )
    assert is_not_enough_balance_error(err)
    gate = parse_balance_gate_error(err)
    assert gate is not None
    assert gate["balance"] == Decimal("3.333332")
    assert gate["matched"] == Decimal("3.33")
    assert abs(gate["free"] - Decimal("0.003332")) < Decimal("0.000001")
    assert gate_has_locked_inventory(gate)
    assert gate_free_cap(gate, Decimal("3.333332")) == gate["free"]
    # Live bal moved → ignore stale free.
    assert gate_free_cap(gate, Decimal("0.05")) is None

    # Bodø first reject: free 0.0358 is still sellable (cap to 0.03).
    bodo = parse_balance_gate_error(
        "not enough balance / allowance: the balance is not enough -> "
        "balance: 3225800, sum of matched orders: 3190000, "
        "order amount (inc. fees): 3190000"
    )
    assert bodo is not None
    assert abs(bodo["free"] - Decimal("0.0358")) < Decimal("0.000001")
    assert not gate_has_locked_inventory(bodo)
    assert flatten_sell_shares_available(bodo["balance"], free=bodo["free"]) == Decimal(
        "0.03"
    )
    assert gate_free_cap(bodo, bodo["balance"]) == bodo["free"]
    # Later tick: free dust while matched still holds bag → keep pending.
    locked = parse_balance_gate_error(
        "not enough balance / allowance: the balance is not enough -> "
        "balance: 35800, sum of matched orders: 3190000, "
        "order amount (inc. fees): 30000"
    )
    assert locked is not None
    assert gate_has_locked_inventory(locked)
    assert (
        flatten_sell_shares_available(Decimal("0.0358"), free=Decimal("0.0058"))
        == Decimal("0")
    )

    print("ok: flatten sell haircut + balance-gate parse + terminal/reason")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Thin py-clob-client-v2 wrapper (mirrors simple_str ClobExecutionClient)."""

from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal
from typing import Any

from eth_account import Account

from trade_settings import TradeSettings

logger = logging.getLogger("pm_quote.clob")

class ClobTrader:
    """Auth once, reuse for market FOK/FAK orders."""

    def __init__(self, settings: TradeSettings) -> None:
        self._settings = settings
        self._client: Any = None
        self._wallet_address: str = ""

    def initialize(self) -> None:
        from py_clob_client_v2 import ClobClient

        if not self._settings.private_key:
            raise RuntimeError("PRIVATE_KEY required for ClobTrader.initialize()")

        kwargs: dict[str, Any] = {
            "host": self._settings.clob_host,
            "key": self._settings.private_key,
            "chain_id": self._settings.chain_id,
            "signature_type": self._settings.signature_type,
        }
        if self._settings.funder:
            kwargs["funder"] = self._settings.funder

        client = ClobClient(**kwargs)
        try:
            creds = client.derive_api_key()
            logger.debug("CLOB API key derived")
        except Exception:
            creds = client.create_api_key()
            logger.info("CLOB API key created (first registration)")
        client.set_api_creds(creds)
        self._client = client
        self._wallet_address = self._resolve_wallet_address()
        # Ensure USDC (collateral) allowance is set before market buys.
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

            client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            logger.info("CLOB collateral allowance updated")
        except Exception as e:  # noqa: BLE001
            logger.warning("CLOB collateral allowance update failed: %s", e)
        logger.info("CLOB ready wallet=%s…", self._wallet_address[:10])

    def _resolve_wallet_address(self) -> str:
        if self._settings.funder:
            return self._settings.funder
        get_addr = getattr(self.client, "get_address", None)
        if callable(get_addr):
            return str(get_addr())
        return Account.from_key(self._settings.private_key).address

    @property
    def wallet_address(self) -> str:
        return self._wallet_address

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("CLOB client not initialized; call initialize()")
        return self._client

    @property
    def ready(self) -> bool:
        return self._client is not None

    def get_conditional_balance(self, token_id: str) -> Decimal:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        result = self.client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        )
        return Decimal(str(result.get("balance", "0"))) / Decimal("1000000")

    def refresh_conditional_allowance(self, token_id: str) -> None:
        """Best-effort refresh of conditional-token allowance before sells."""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        try:
            self.client.update_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL, token_id=str(token_id)
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "conditional allowance update failed token=%s…: %s",
                str(token_id)[:12],
                e,
            )

    def cancel_orders_for_asset(self, token_id: str) -> Any:
        """Cancel open orders for one outcome token (frees locked shares)."""
        from py_clob_client_v2.clob_types import OrderMarketCancelParams

        tid = str(token_id)
        try:
            out = self.client.cancel_market_orders(
                OrderMarketCancelParams(asset_id=tid)
            )
            logger.info("canceled market orders asset=%s… result=%s", tid[:12], out)
            return out
        except Exception as e:  # noqa: BLE001
            # Fallback: cancel individually from open-order list.
            logger.warning(
                "cancel_market_orders failed token=%s… (%s); trying get_open_orders",
                tid[:12],
                e,
            )
        try:
            from py_clob_client_v2.clob_types import OpenOrderParams, OrderPayload

            opens = self.client.get_open_orders(OpenOrderParams(asset_id=tid)) or []
            ids = [
                str(o.get("id") or o.get("orderID") or o.get("order_id") or "")
                for o in opens
                if isinstance(o, dict)
            ]
            ids = [i for i in ids if i]
            if not ids:
                return {"canceled": 0}
            if len(ids) == 1:
                return self.client.cancel_order(OrderPayload(orderID=ids[0]))
            return self.client.cancel_orders(ids)
        except Exception as e2:  # noqa: BLE001
            logger.warning("cancel open orders failed token=%s…: %s", tid[:12], e2)
            return None

    @staticmethod
    def is_order_success(result: dict | None, *, market: bool = True) -> bool:
        """Whether a market order was accepted / filled.

        MATCHED/FILLED = done. DELAYED = CLOB accepted (async settle) — treat as
        success so buy_win lots enter the open ledger for score-reversal flatten.
        Do not treat resting LIVE/OPEN limit orders as fills.
        """
        if not result:
            return False
        status = str(result.get("status", "")).upper()
        has_id = bool(result.get("orderID") or result.get("id"))
        if market:
            if status in ("MATCHED", "FILLED", "SUCCESS"):
                return True
            # Polymarket often returns delayed + success=true before shares show up.
            if status == "DELAYED" and (result.get("success") is True or has_id):
                return True
            if result.get("success") is True and status not in ("LIVE", "OPEN"):
                return True
            return False
        if status in ("MATCHED", "FILLED", "LIVE", "OPEN", "SUCCESS", "DELAYED"):
            return True
        if result.get("success") is True:
            return True
        if has_id:
            err = result.get("errorMsg") or result.get("error")
            return not err
        return False

    def wait_conditional_balance(
        self,
        token_id: str,
        *,
        min_shares: float = 0.01,
        timeout_s: float = 3.0,
        interval_s: float = 0.35,
    ) -> float:
        """Poll conditional token balance until visible or timeout (delayed fills)."""
        import time

        deadline = time.time() + max(0.0, float(timeout_s))
        last = 0.0
        while True:
            try:
                last = float(self.get_conditional_balance(token_id))
            except Exception:  # noqa: BLE001
                last = 0.0
            if last + 1e-12 >= float(min_shares):
                return last
            if time.time() >= deadline:
                return last
            time.sleep(max(0.05, float(interval_s)))

    def post_market_buy(
        self,
        token_id: str,
        amount_usdc: Decimal,
        tick_size: str,
        max_price: Decimal,
        *,
        order_type: str = "FAK",
        neg_risk: bool | None = None,
    ) -> dict:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side
        from py_clob_client_v2.clob_types import PartialCreateOrderOptions

        if max_price is None or max_price <= 0:
            raise ValueError("BUY requires max_price")
        ot = OrderType.FOK if str(order_type).upper() == "FOK" else OrderType.FAK
        # py-clob-client-v2: OrderType members are plain strings (no .name).
        ot_label = getattr(ot, "name", None) or str(ot)
        options = PartialCreateOrderOptions(tick_size=tick_size)
        if neg_risk is not None:
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=bool(neg_risk))
        args = MarketOrderArgs(
            token_id=token_id,
            amount=float(amount_usdc),
            side=Side.BUY,
            order_type=ot,
            price=float(max_price),
        )
        result = self.client.create_and_post_market_order(
            args, options=options, order_type=ot
        )
        out = result if isinstance(result, dict) else {"result": result}
        logger.info(
            "market BUY %s token=%s usdc=%s max_price=%s ok=%s",
            ot_label,
            token_id[:12],
            amount_usdc,
            max_price,
            self.is_order_success(out),
        )
        return out

    def post_market_sell(
        self,
        token_id: str,
        shares: Decimal,
        tick_size: str,
        *,
        min_price: Decimal | None = None,
        order_type: str = "FAK",
        neg_risk: bool | None = None,
    ) -> dict:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, Side
        from py_clob_client_v2.clob_types import PartialCreateOrderOptions

        ot = OrderType.FOK if str(order_type).upper() == "FOK" else OrderType.FAK
        ot_label = getattr(ot, "name", None) or str(ot)
        options = PartialCreateOrderOptions(tick_size=tick_size)
        if neg_risk is not None:
            options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=bool(neg_risk))
        sell_shares = Decimal(str(shares)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if sell_shares <= 0:
            raise ValueError(f"sell shares floor to 0 from {shares}")
        kwargs: dict[str, Any] = {
            "token_id": token_id,
            "amount": float(sell_shares),
            "side": Side.SELL,
            "order_type": ot,
        }
        if min_price is not None and min_price > 0:
            kwargs["price"] = float(min_price)
        else:
            kwargs["price"] = 0.01
        args = MarketOrderArgs(**kwargs)
        result = self.client.create_and_post_market_order(
            args, options=options, order_type=ot
        )
        out = result if isinstance(result, dict) else {"result": result}
        logger.info(
            "market SELL %s token=%s shares=%s min_price=%s ok=%s",
            ot_label,
            token_id[:12],
            sell_shares,
            min_price,
            self.is_order_success(out),
        )
        return out

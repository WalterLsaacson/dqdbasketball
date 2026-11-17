"""Trade settings: env auth (simple_str pattern) + fill depth caps."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from size_policy import (
    DEFAULT_SIZE_TIERS,
    format_size_tiers,
    parse_size_tiers,
)


def parse_trade_mode(mode: str | None, *, default_live: bool) -> bool:
    """Map dry|live (or None) to bool; None keeps ``default_live``."""
    if mode is None or str(mode).strip() == "":
        return bool(default_live)
    m = str(mode).strip().lower()
    if m not in ("dry", "live"):
        raise ValueError(f"trade mode must be 'dry' or 'live', got {mode!r}")
    return m == "live"


def resolve_live_modes(
    *,
    live: bool = False,
    score_mode: str | None = None,
    prop_mode: str | None = None,
    ft_mode: str | None = None,
) -> tuple[bool, bool, bool]:
    """Return score, player-prop, and FT live flags."""
    base = bool(live)
    return (
        parse_trade_mode(score_mode, default_live=base),
        parse_trade_mode(prop_mode, default_live=base),
        parse_trade_mode(ft_mode, default_live=base),
    )


@dataclass(frozen=True)
class TradeSettings:
    """CLOB auth + in-process execution knobs."""

    private_key: str
    funder: str | None
    signature_type: int
    chain_id: int
    clob_host: str
    data_api_url: str

    live_score: bool  # score_change
    live_prop: bool  # player_stat_change
    live_ft: bool  # match_finished
    take_depth: str  # "top" | "walk"
    max_levels: int
    max_usdc: float
    max_shares: float
    max_slippage: float
    allow_extreme_prices: bool
    min_buy_price: float  # buy_win only: skip (but record) when best_ask < this; 0 = off
    min_order_shares: float
    enabled: bool
    # Price-tiered buy sizing (from .env); hard caps remain max_usdc/max_shares.
    size_tiers: tuple[tuple[float, float], ...]
    max_open_usdc: float
    size_floor_usdc: float

    @property
    def live(self) -> bool:
        """True if either signal channel posts real CLOB orders."""
        return bool(self.live_score or self.live_prop or self.live_ft)


def _repo_root_from(here: Path) -> Path:
    # .../.cursor/skills/polymarket-quote/scripts/trade_settings.py → repo root
    return here.resolve().parents[4]


def load_trade_settings(
    *,
    live: bool = False,
    live_score: bool | None = None,
    live_prop: bool | None = None,
    live_ft: bool | None = None,
    score_mode: str | None = None,
    prop_mode: str | None = None,
    ft_mode: str | None = None,
    take_depth: str = "top",
    max_levels: int = 5,
    max_usdc: float | None = None,
    max_shares: float | None = None,
    max_slippage: float = 0.03,
    allow_extreme_prices: bool = False,
    min_buy_price: float = 0.92,
    enabled: bool = True,
    env_file: str | Path | None = None,
    require_key: bool = False,
) -> TradeSettings:
    """Load PRIVATE_KEY / FUNDER / … from repo .env or --trade-env-file."""
    here = Path(__file__).resolve()
    root = _repo_root_from(here)
    path = Path(env_file) if env_file else root / ".env"
    if path.is_file():
        load_dotenv(path, override=False)

    if live_score is not None or live_prop is not None or live_ft is not None:
        s = bool(live_score) if live_score is not None else bool(live)
        p = bool(live_prop) if live_prop is not None else bool(live)
        f = bool(live_ft) if live_ft is not None else bool(live)
    else:
        s, p, f = resolve_live_modes(
            live=live, score_mode=score_mode, prop_mode=prop_mode, ft_mode=ft_mode
        )

    private_key = os.getenv("PRIVATE_KEY", "").strip()
    if require_key and not private_key:
        raise ValueError(
            "PRIVATE_KEY not set; copy .env with PRIVATE_KEY/FUNDER "
            "(same names as simple_str) or pass --trade-env-file"
        )

    depth = (take_depth or "top").strip().lower()
    if depth not in ("top", "walk"):
        raise ValueError(f"take_depth must be 'top' or 'walk', got {take_depth!r}")

    # Hard caps: .env QUOTE_MAX_* wins when set; else CLI/hub arg; else defaults.
    if os.getenv("QUOTE_MAX_USDC"):
        hard_usdc = float(os.getenv("QUOTE_MAX_USDC") or 20)
    elif max_usdc is not None:
        hard_usdc = float(max_usdc)
    else:
        hard_usdc = 20.0
    if os.getenv("QUOTE_MAX_SHARES"):
        hard_shares = float(os.getenv("QUOTE_MAX_SHARES") or 25)
    elif max_shares is not None:
        hard_shares = float(max_shares)
    else:
        hard_shares = 25.0

    tiers = parse_size_tiers(os.getenv("QUOTE_SIZE_TIERS"))
    max_open = float(os.getenv("QUOTE_MAX_OPEN_USDC", "1000") or 1000)
    floor_usdc = float(os.getenv("QUOTE_SIZE_FLOOR_USDC", "1") or 1)

    funder = os.getenv("FUNDER", "").strip() or None
    return TradeSettings(
        private_key=private_key,
        funder=funder,
        signature_type=int(os.getenv("SIGNATURE_TYPE", "2")),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com").rstrip("/"),
        data_api_url=os.getenv("DATA_API_URL", "https://data-api.polymarket.com").rstrip("/"),
        live_score=s,
        live_prop=p,
        live_ft=f,
        take_depth=depth,
        max_levels=max(1, int(max_levels)),
        max_usdc=hard_usdc,
        max_shares=hard_shares,
        max_slippage=float(max_slippage),
        allow_extreme_prices=bool(allow_extreme_prices),
        min_buy_price=max(0.0, float(min_buy_price)),
        # Polymarket market buys are USDC-notional; do not impose a 5-share floor.
        min_order_shares=float(os.getenv("MIN_ORDER_SHARES", "0")),
        enabled=bool(enabled),
        size_tiers=tuple(tiers) if tiers else tuple(DEFAULT_SIZE_TIERS),
        max_open_usdc=max(0.0, max_open),
        size_floor_usdc=max(0.0, floor_usdc),
    )


def size_tiers_label(settings: TradeSettings) -> str:
    return format_size_tiers(settings.size_tiers)

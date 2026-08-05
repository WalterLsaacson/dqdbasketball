#!/usr/bin/env python3
"""CLI for basketball Polymarket CLOB quoting from match-bridge events.

Examples:
  python3 pm_quote.py once --from-bridge --json
  python3 pm_quote.py once --match-id 54363289 --home-score 2 --away-score 1 --json
  python3 pm_quote.py once --event-id 674336 --home-score 1 --away-score 0 --json
  python3 pm_quote.py watch --interval 0.25
  python3 pm_quote.py watch --take-depth walk --max-usdc 10   # dry-run trade plans
  python3 pm_quote.py watch --live --take-depth top --max-usdc 2
  python3 pm_quote.py watch --score-mode dry --prop-mode live --ft-mode live
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import data_prune  # noqa: E402
import market_cache as mcache  # noqa: E402
import quote_lib as lib  # noqa: E402
from post_goal_sampler import PostGoalSampler  # noqa: E402
from trade_executor import TradeExecutor  # noqa: E402
from trade_settings import load_trade_settings, resolve_live_modes, size_tiers_label  # noqa: E402


def root() -> Path:
    return lib.repo_root_from(Path(__file__))


def build_executor(args: argparse.Namespace, rt: Path) -> TradeExecutor | None:
    """Build TradeExecutor when trading is enabled (default on; --no-trade disables)."""
    if getattr(args, "no_trade", False):
        return None
    live = bool(getattr(args, "live", False))
    score_mode = getattr(args, "score_mode", None)
    prop_mode = getattr(args, "prop_mode", None)
    ft_mode = getattr(args, "ft_mode", None)
    # Peek modes so require_key matches any live channel.
    live_score, live_prop, live_ft = resolve_live_modes(
        live=live, score_mode=score_mode, prop_mode=prop_mode, ft_mode=ft_mode
    )
    settings = load_trade_settings(
        live=live,
        score_mode=score_mode,
        prop_mode=prop_mode,
        ft_mode=ft_mode,
        take_depth=str(getattr(args, "take_depth", "top") or "top"),
        max_levels=int(getattr(args, "max_levels", 5)),
        max_usdc=float(getattr(args, "max_usdc", 20.0)),
        max_shares=float(getattr(args, "max_shares", 25.0)),
        max_slippage=float(getattr(args, "max_slippage", 0.03)),
        allow_extreme_prices=bool(getattr(args, "allow_extreme_prices", False)),
        min_buy_price=float(getattr(args, "min_buy_price", 0.92)),
        enabled=True,
        env_file=getattr(args, "trade_env_file", None),
        require_key=bool(live_score or live_prop or live_ft),
    )
    executor = TradeExecutor(rt, settings)
    # Plan: initialize ClobClient once at watch/start when trading is on (reuse).
    # Position client still used for flatten / open-lot balance checks.
    if settings.private_key:
        try:
            executor.ensure_trader()
        except Exception as e:  # noqa: BLE001
            if settings.live:
                raise
            print(
                f"trade → CLOB init failed (sell position checks disabled): {e}",
                file=sys.stderr,
                flush=True,
            )
    s = "live" if settings.live_score else "dry"
    p = "live" if settings.live_prop else "dry"
    f = "live" if settings.live_ft else "dry"
    print(
        f"trade → score={s} props={p} ft={f} take_depth={settings.take_depth} "
        f"max_usdc={settings.max_usdc} max_shares={settings.max_shares} "
        f"min_buy_price={settings.min_buy_price} "
        f"max_open_usdc={settings.max_open_usdc} "
        f"size_tiers={size_tiers_label(settings)}",
        file=sys.stderr,
        flush=True,
    )
    return executor


def cmd_once(args: argparse.Namespace) -> int:
    rt = root()
    proxy = None if args.no_proxy else (args.proxy if args.proxy else ...)
    include_props = not args.moneyline_only
    include_exact = not args.moneyline_only and not args.no_exact
    try:
        executor = build_executor(args, rt)
    except Exception as e:  # noqa: BLE001
        print(f"trade setup failed: {e}", file=sys.stderr)
        return 1
    try:
        if args.from_bridge:
            cache = mcache.MarketCatalogCache(rt)
            bundles = lib.process_bridge_events(
                rt,
                proxy=proxy,
                include_props=include_props,
                include_exact=include_exact,
                eps=float(args.eps),
                fee_rate=float(args.fee_rate),
                min_net=float(args.min_net),
                force=bool(args.force),
                trade_executor=executor,
                market_cache=cache,
            )
            payload = {
                "quoted_at": lib.now_cn_iso(),
                "source": "polymarket-quote",
                "mode": "from_bridge",
                "count": len(bundles),
                "bundles": bundles,
            }
        else:
            row = lib.find_match_row(
                rt, match_id=args.match_id, event_id=args.event_id
            )
            if not row:
                print(
                    "match not found in data/bridge/matches.json; "
                    "pass --match-id or --event-id of a bridged fixture",
                    file=sys.stderr,
                )
                return 1
            if args.home_score is None or args.away_score is None:
                dqd = row.get("dongqiudi") or {}
                hs, aws = dqd.get("home_score"), dqd.get("away_score")
                if hs is None or aws is None:
                    print(
                        "need --home-score and --away-score (fixture not finished in bridge)",
                        file=sys.stderr,
                    )
                    return 1
            else:
                hs, aws = int(args.home_score), int(args.away_score)
            ev = lib.synthetic_ft_from_row(row, home_score=int(hs), away_score=int(aws))
            cache = mcache.MarketCatalogCache(rt)
            payload = lib.quote_finished_event(
                rt,
                ev,
                proxy=proxy,
                include_props=include_props,
                include_exact=include_exact,
                eps=float(args.eps),
                fee_rate=float(args.fee_rate),
                min_net=float(args.min_net),
                persist=not args.no_persist,
                trade_executor=executor,
                market_cache=cache,
            )
    except Exception as e:  # noqa: BLE001
        print(f"quote failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        if isinstance(payload, dict) and "bundles" in payload:
            print(
                f"processed {payload.get('count')} FT events → data/pm-quote/",
                flush=True,
            )
        else:
            print(
                f"quoted {payload.get('home')} {payload.get('home_score')}-"
                f"{payload.get('away_score')} {payload.get('away')} "
                f"tokens={payload.get('count')} opps={payload.get('opportunity_count')}",
                flush=True,
            )
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    rt = root()
    proxy = None if args.no_proxy else (args.proxy if args.proxy else ...)
    include_props = not args.moneyline_only
    include_exact = not args.moneyline_only and not args.no_exact
    interval = max(0.05, float(args.interval))
    events_path = lib.bridge_dir(rt) / "events.jsonl"
    cache = mcache.MarketCatalogCache(rt)
    stop_warm = threading.Event()

    if not args.no_upstream:
        print(
            "upstream → starting match-bridge in-process "
            "(dongqiudi-match + polymarket-soccer · memory event queue)…",
            file=sys.stderr,
            flush=True,
        )
        try:
            up = lib.ensure_upstream_bridge(rt)
            mode = up.get("mode")
            already = "already running" if up.get("already") else "started"
            if mode == "bridge_board":
                print(
                    f"upstream → bridge-board {already} @ {up.get('url')} "
                    f"(DQD ticks={up.get('dqd_ticks')} · PM ticks={up.get('pm_ticks')}) "
                    f"[file wake]",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"upstream → match-bridge {already} in-process "
                    f"(DQD {up.get('dqd_interval')}/{up.get('dqd_idle_interval')}s · "
                    f"PM {up.get('pm_interval')}s · event_queue)",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as e:  # noqa: BLE001
            print(f"upstream → failed to start bridge: {e}", file=sys.stderr, flush=True)
            return 1

    try:
        executor = build_executor(args, rt)
    except Exception as e:  # noqa: BLE001
        print(f"trade setup failed: {e}", file=sys.stderr)
        return 1
    # Configure process proxy once before warmer + quote share SOCKS socket patch.
    try:
        if proxy is None:
            lib.pm.configure_proxy(None)  # honor --no-proxy → direct
        elif proxy is ...:
            lib.pm.configure_proxy(None)  # default PM_PROXY
        else:
            lib.pm.configure_proxy(str(proxy))
    except Exception as e:  # noqa: BLE001
        print(f"proxy setup warning: {e}", file=sys.stderr, flush=True)

    mcache.start_warmer(cache, proxy=proxy, interval_s=5.0, stop_event=stop_warm)
    retain_h = float(getattr(args, "retain_hours", data_prune.DEFAULT_RETAIN_HOURS))
    data_prune.start_pruner(
        rt,
        market_cache=cache,
        retain_hours=retain_h,
        interval_s=600.0,
        stop_event=stop_warm,
        run_immediately=True,
    )
    sampler = PostGoalSampler(rt)
    sampler.start()
    print(
        f"post-goal sampler → {lib.data_dir(rt) / 'post_goal_samples.jsonl'} "
        f"(buy tokens · 10s × 6 · jsonl only)",
        file=sys.stderr,
        flush=True,
    )
    print(
        f"polymarket-quote watch (wake≤{interval}s · memory queue|file · "
        f"market_cache · retain={retain_h}h) → {lib.data_dir(rt)}",
        file=sys.stderr,
        flush=True,
    )
    sig = mcache.file_signature(events_path)
    owned = lib.get_owned_bridge()
    first_tick = True
    try:
        while True:
            mem_events: list = []
            if not first_tick:
                if owned is not None:
                    mem_events = owned.wait_events(interval)
                else:
                    sig = mcache.wait_for_file_change(
                        events_path, sig, timeout_s=interval, poll_s=0.05
                    )
            first_tick = False

            bundles = lib.process_bridge_events(
                rt,
                proxy=proxy,
                include_props=include_props,
                include_exact=include_exact,
                eps=float(args.eps),
                fee_rate=float(args.fee_rate),
                min_net=float(args.min_net),
                force=False,
                trade_executor=executor,
                market_cache=cache,
                events_override=mem_events if mem_events else None,
            )
            for b in bundles:
                lat = b.get("latency_ms") or {}
                lat_s = ""
                if lat:
                    parts = [
                        f"{k}={v}ms" if isinstance(v, int) else f"{k}={v}"
                        for k, v in lat.items()
                    ]
                    lat_s = " · " + " ".join(parts)
                if b.get("error"):
                    print(
                        f"error match_id={b.get('match_id')} "
                        f"trigger={b.get('trigger')}: {b.get('error')}{lat_s}",
                        flush=True,
                    )
                else:
                    prev = b.get("prev_score") or {}
                    prev_s = (
                        f"{prev.get('home')}-{prev.get('away')}→"
                        if prev.get("home") is not None
                        else ""
                    )
                    trades = sum(
                        1
                        for q in (b.get("quotes") or [])
                        if (q.get("trade_attempt") or {}).get("status")
                        in ("dry_run", "posted")
                    )
                    flat = int(b.get("flatten_count") or 0)
                    flat_s = f" flatten={flat}" if flat else ""
                    cache_s = ""
                    disc = b.get("discovery") or {}
                    if disc.get("catalog_cache"):
                        cache_s = f" cache={disc.get('catalog_cache')}"
                    if disc.get("books_once"):
                        cache_s += " books=once"
                    print(
                        f"[{b.get('quoted_at')}] {b.get('trigger')}/{b.get('mode')} "
                        f"{b.get('home')} {prev_s}{b.get('home_score')}-{b.get('away_score')} "
                        f"{b.get('away')} quotes={b.get('count')} "
                        f"opps={b.get('opportunity_count')} trades={trades}"
                        f"{flat_s}{cache_s}{lat_s}",
                        flush=True,
                    )
                    if args.json:
                        json.dump(b, sys.stdout, ensure_ascii=False)
                        print(flush=True)
    except KeyboardInterrupt:
        print("\nbye", file=sys.stderr)
        stop_warm.set()
        sampler.stop()
        if not args.no_upstream:
            lib.stop_owned_bridge()
        return 0


def _add_common_flags(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--proxy", default=None, help="Proxy URL (default PM_PROXY / 127.0.0.1:1082)")
    sp.add_argument("--no-proxy", action="store_true", help="Disable proxy")
    sp.add_argument("--eps", type=float, default=0.005, help="Min gross edge vs 0/1 before fee check")
    sp.add_argument(
        "--fee-rate",
        type=float,
        default=lib.SPORTS_TAKER_FEE_RATE,
        help=f"Sports taker feeRate (default {lib.SPORTS_TAKER_FEE_RATE})",
    )
    sp.add_argument(
        "--min-net",
        type=float,
        default=lib.DEFAULT_MIN_NET,
        help=f"Min net edge/share after fee to write opportunities (default {lib.DEFAULT_MIN_NET})",
    )
    sp.add_argument(
        "--moneyline-only",
        action="store_true",
        help="Skip More Markets / Exact Score discovery",
    )
    sp.add_argument("--no-exact", action="store_true", help="Skip Exact Score only")
    # --- in-process trading (after misprice; default dry-run) ---
    sp.add_argument(
        "--no-trade",
        action="store_true",
        help="Disable in-process trading (quote only)",
    )
    sp.add_argument(
        "--live",
        action="store_true",
        help="Post real CLOB orders for score, player-stat, and FT signals",
    )
    sp.add_argument(
        "--score-mode",
        choices=("dry", "live"),
        default=None,
        help="score_change dry|live (default dry; --live sets live unless overridden)",
    )
    sp.add_argument(
        "--prop-mode",
        choices=("dry", "live"),
        default=None,
        help="player_stat_change dry|live (default dry; --live sets live unless overridden)",
    )
    sp.add_argument(
        "--ft-mode",
        choices=("dry", "live"),
        default=None,
        help="match_finished dry|live (default dry; --live sets live unless overridden)",
    )
    sp.add_argument(
        "--take-depth",
        choices=("top", "walk"),
        default="top",
        help="top=best level only; walk=deeper into asks_top/bids_top (default top)",
    )
    sp.add_argument(
        "--max-levels",
        type=int,
        default=5,
        help="Walk max book levels (default 5, matches TOP_N)",
    )
    sp.add_argument("--max-usdc", type=float, default=20.0, help="Hard max USDC per buy (default 20; .env QUOTE_MAX_USDC wins)")
    sp.add_argument(
        "--max-shares",
        type=float,
        default=25.0,
        help="Hard max shares per order (default 25; .env QUOTE_MAX_SHARES wins; scaled with usdc by ask)",
    )
    sp.add_argument(
        "--max-slippage",
        type=float,
        default=0.03,
        help="Walk: max adverse price move from best (default 0.03)",
    )
    sp.add_argument(
        "--allow-extreme-prices",
        action="store_true",
        help="Allow orders when best price <=0.01 or >=0.99 (default blocked)",
    )
    sp.add_argument(
        "--min-buy-price",
        type=float,
        default=0.92,
        help="buy_win: skip (still log trades.jsonl) when best_ask < this (default 0.92; 0=off)",
    )
    sp.add_argument(
        "--trade-env-file",
        default=None,
        help="Env file with PRIVATE_KEY/FUNDER/… (default repo .env)",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Post-FT Polymarket CLOB quoting")
    sub = p.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="Quote once (bridge backlog or single match)")
    _add_common_flags(once)
    once.add_argument("--from-bridge", action="store_true", help="Process new bridge events")
    once.add_argument("--force", action="store_true", help="Re-process already-seen bridge keys")
    once.add_argument("--match-id", default=None, help="Dongqiudi match id")
    once.add_argument("--event-id", default=None, help="Polymarket event id")
    once.add_argument("--home-score", type=int, default=None)
    once.add_argument("--away-score", type=int, default=None)
    once.add_argument("--no-persist", action="store_true")
    once.add_argument("--json", action="store_true")
    once.set_defaults(func=cmd_once)

    watch = sub.add_parser(
        "watch",
        help="Poll bridge events and quote; autostarts match-bridge (+ DQD + PM)",
    )
    _add_common_flags(watch)
    watch.add_argument(
        "--interval",
        type=float,
        default=0.25,
        help="Max idle seconds between ticks; wakes early on events.jsonl (default 0.25)",
    )
    watch.add_argument("--json", action="store_true", help="Emit each bundle as JSON line")
    watch.add_argument(
        "--no-upstream",
        action="store_true",
        help="Do not autostart match-bridge / dongqiudi-match / polymarket-soccer",
    )
    watch.add_argument(
        "--retain-hours",
        type=float,
        default=data_prune.DEFAULT_RETAIN_HOURS,
        help="Rolling retention hours for jsonl + stale market_cache (default 24)",
    )
    watch.set_defaults(func=cmd_watch)

    prune = sub.add_parser(
        "prune",
        help="Prune market_cache + jsonl older than rolling retain window",
    )
    prune.add_argument(
        "--retain-hours",
        type=float,
        default=data_prune.DEFAULT_RETAIN_HOURS,
        help="Rolling hours to keep (default 24; not calendar-day)",
    )
    prune.add_argument("--json", action="store_true", help="Print prune report as JSON")
    prune.set_defaults(func=cmd_prune)

    return p


def cmd_prune(args: argparse.Namespace) -> int:
    rt = root()
    report = data_prune.prune_runtime_data(
        rt, retain_hours=float(args.retain_hours)
    )
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        mc = report.get("market_cache") or {}
        print(
            f"prune cutoff={report.get('cutoff')} "
            f"cache_dropped={mc.get('dropped')}/{mc.get('scanned')} "
            f"active_open={mc.get('active_open')}",
            flush=True,
        )
        for name, stats in (report.get("jsonl") or {}).items():
            if not isinstance(stats, dict):
                continue
            if stats.get("error"):
                print(f"  {name}: error {stats['error']}", flush=True)
            else:
                print(
                    f"  {name}: kept={stats.get('kept')} removed={stats.get('removed')} "
                    f"{stats.get('bytes_before')}→{stats.get('bytes_after')} bytes",
                    flush=True,
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

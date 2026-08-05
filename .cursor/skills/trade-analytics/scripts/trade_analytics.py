#!/usr/bin/env python3
"""CLI for historical polymarket-quote trade analytics.

Examples:
  python3 trade_analytics.py summary
  python3 trade_analytics.py summary --last-hours 12
  python3 trade_analytics.py summary --since 2026-07-22T23:20:00+08:00 --json
  python3 trade_analytics.py list --trade buy_win --last-hours 24
  python3 trade_analytics.py opens
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import analytics_lib as lib  # noqa: E402


def cmd_summary(args: argparse.Namespace) -> int:
    since, until = lib.resolve_window(
        since=args.since,
        until=args.until,
        last_hours=args.last_hours,
        last_days=args.last_days,
    )
    report = lib.build_report(
        lib.repo_root(),
        trades_path=Path(args.trades) if args.trades else None,
        opens_path=Path(args.opens) if args.opens else None,
        since=since,
        until=until,
        persist=not args.no_persist,
    )
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print(lib.format_summary_text(report))
        if report.get("report_path"):
            print(f"\nwrote {report['report_path']}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = lib.repo_root()
    tpath = Path(args.trades) if args.trades else root / lib.DEFAULT_TRADES
    since, until = lib.resolve_window(
        since=args.since,
        until=args.until,
        last_hours=args.last_hours,
        last_days=args.last_days,
    )
    rows = lib.filter_window(lib.load_jsonl(tpath), since=since, until=until)
    if args.trade:
        rows = [r for r in rows if r.get("trade") == args.trade]
    if args.status:
        rows = [r for r in rows if r.get("status") == args.status]
    if args.family:
        rows = [r for r in rows if r.get("family") == args.family]
    if args.match_id:
        rows = [r for r in rows if str(r.get("match_id")) == str(args.match_id)]

    compact = [lib.compact_trade(r) for r in rows]
    if args.limit is not None:
        compact = compact[-int(args.limit) :]

    if args.json:
        json.dump(
            {"count": len(compact), "trades": compact},
            sys.stdout,
            ensure_ascii=False,
            indent=2,
        )
        print()
        return 0

    print(f"trades={len(compact)}")
    for t in compact:
        est = t.get("est_profit")
        est_s = f" est=+{est:.3f}" if isinstance(est, (int, float)) else ""
        skip = f" skip={t.get('skip_reason')}" if t.get("skip_reason") else ""
        print(
            f"{t.get('quoted_at')}  {t.get('status')}/{t.get('trade')}  "
            f"{t.get('home')} {t.get('score')} {t.get('away')}  "
            f"{t.get('family')}/{t.get('outcome')}  "
            f"usdc={t.get('usdc')}{est_s}{skip}"
        )
    return 0


def cmd_opens(args: argparse.Namespace) -> int:
    root = lib.repo_root()
    opath = Path(args.opens) if args.opens else root / lib.DEFAULT_OPENS
    lots = lib.load_open_positions(opath)
    if args.status:
        lots = [x for x in lots if x.get("status") == args.status]
    if args.json:
        json.dump({"count": len(lots), "positions": lots}, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    from collections import Counter

    print(f"positions={len(lots)}  status={dict(Counter(x.get('status') for x in lots))}")
    for x in lots:
        entry = x.get("entry_score")
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            entry_s = f"{entry[0]}-{entry[1]}"
        else:
            entry_s = str(entry)
        print(
            f"{x.get('status')}  {x.get('home')} vs {x.get('away')}  "
            f"entry={entry_s}  {x.get('family')}/{x.get('market_key')}  "
            f"shares={x.get('shares')} usdc={x.get('usdc')}"
        )
    return 0


def _add_window_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--since", default=None, help="ISO time (CN ok), inclusive")
    p.add_argument("--until", default=None, help="ISO time, inclusive")
    p.add_argument("--last-hours", type=float, default=None)
    p.add_argument("--last-days", type=float, default=None)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Historical trade analytics for pm-quote ledgers")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sum = sub.add_parser("summary", help="PnL / counts / flatten summary")
    _add_window_flags(p_sum)
    p_sum.add_argument("--trades", default=None, help="Override trades.jsonl path")
    p_sum.add_argument("--opens", default=None, help="Override open_positions.json path")
    p_sum.add_argument("--json", action="store_true")
    p_sum.add_argument("--no-persist", action="store_true", help="Do not write data/trade-analytics/latest.json")
    p_sum.set_defaults(func=cmd_summary)

    p_list = sub.add_parser("list", help="List compact trade rows")
    _add_window_flags(p_list)
    p_list.add_argument("--trades", default=None)
    p_list.add_argument("--trade", default=None, help="buy_win|sell_lose|flatten_reversal")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--family", default=None)
    p_list.add_argument("--match-id", default=None)
    p_list.add_argument("--limit", type=int, default=None)
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_open = sub.add_parser("opens", help="Show open_positions ledger")
    p_open.add_argument("--opens", default=None)
    p_open.add_argument("--status", default=None, help="Filter e.g. open")
    p_open.add_argument("--json", action="store_true")
    p_open.set_defaults(func=cmd_opens)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

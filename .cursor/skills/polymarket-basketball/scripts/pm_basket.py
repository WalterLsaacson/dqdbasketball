#!/usr/bin/env python3
"""CLI for Polymarket basketball match lists via the public Gamma API."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pm_lib as lib  # noqa: E402


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def data_dir(explicit: str | None = None) -> Path:
    root = Path(explicit) if explicit else repo_root() / "data" / "polymarket"
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_leagues_arg(raw: str | None) -> list[str] | None:
    if not raw or raw.strip().lower() in ("all", "*"):
        return None
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def resolve_cli_proxy(args: argparse.Namespace) -> str | None | object:
    if getattr(args, "no_proxy", False):
        return "none"
    if args.proxy is not None:
        return args.proxy
    return ...


def cmd_leagues(args: argparse.Namespace) -> int:
    proxy = resolve_cli_proxy(args)
    try:
        catalog = lib.basketball_league_catalog(proxy=proxy)
        proxy_url = lib.resolve_proxy(None if proxy is ... else (None if proxy == "none" else str(proxy)))
    except lib.FetchError as error:
        print(f"fetch failed: {error}", file=sys.stderr)
        return 1
    payload = {
        "fetched_at": datetime.now(lib.TZ_CN).isoformat(timespec="seconds"),
        "source": "polymarket-gamma",
        "proxy": proxy_url or "direct",
        "count": len(catalog),
        "leagues": catalog,
    }
    write_json(data_dir(args.data_dir) / "leagues.json", payload)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    try:
        payload = lib.load_matches(
            parse_leagues_arg(args.league),
            include_closed=args.include_closed,
            max_per_league=args.max,
            within_hours=args.within_hours,
            proxy=resolve_cli_proxy(args),
        )
    except lib.FetchError as error:
        print(f"fetch failed: {error}", file=sys.stderr)
        return 1
    write_json(data_dir(args.data_dir) / "snapshot.json", payload)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket basketball games via Gamma API")
    parser.add_argument("--data-dir", default=None, help="State directory (default: <repo>/data/polymarket)")
    parser.add_argument(
        "--proxy",
        default=None,
        help="Outbound proxy URL (default: http://127.0.0.1:1082); also PM_PROXY / ALL_PROXY",
    )
    parser.add_argument("--no-proxy", action="store_true", help="Disable proxy (direct HTTPS)")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="Fetch basketball matchups and markets as JSON")
    list_parser.add_argument("--league", default="all", help="League code(s), comma-separated (default: all)")
    list_parser.add_argument("--max", type=int, default=100, help="Max events to fetch per league (default: 100)")
    list_parser.add_argument("--include-closed", action="store_true", help="Include closed events")
    list_parser.add_argument(
        "--within-hours",
        type=int,
        default=lib.DEFAULT_WITHIN_HOURS,
        help=f"Fixtures in the next N hours (default: {lib.DEFAULT_WITHIN_HOURS}); 0 disables window",
    )
    list_parser.add_argument("--json", action="store_true", default=True, help="Emit JSON (default)")
    list_parser.set_defaults(func=cmd_list)

    leagues_parser = sub.add_parser("leagues", help="List basketball league codes from Gamma")
    leagues_parser.add_argument("--json", action="store_true", default=True)
    leagues_parser.set_defaults(func=cmd_leagues)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""CLI for match-bridge: run DQD + Polymarket skills and emit matched market handles.

Examples:
  python3 bridge_match.py once --json
  python3 bridge_match.py start
  python3 bridge_match.py status --json
  python3 bridge_match.py stop
  python3 bridge_match.py list --json
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import bridge_lib as lib  # noqa: E402

_RUNTIME: lib.BridgeRuntime | None = None


def root() -> Path:
    return lib.repo_root_from(Path(__file__))


def get_runtime(args: argparse.Namespace) -> lib.BridgeRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = lib.BridgeRuntime(
            root(),
            dqd_tab=getattr(args, "tab", "full") or "full",
            dqd_interval=int(getattr(args, "dqd_interval", 15) or 15),
            dqd_idle_interval=int(getattr(args, "dqd_idle_interval", 60) or 60),
            pm_interval=int(getattr(args, "pm_interval", 600) or 600),
            pm_within_hours=int(getattr(args, "within_hours", 48) or 48),
            min_score=float(getattr(args, "min_score", lib.DEFAULT_MIN_SCORE) or lib.DEFAULT_MIN_SCORE),
            max_skew_min=int(getattr(args, "max_skew_min", lib.DEFAULT_MAX_SKEW_MIN) or lib.DEFAULT_MAX_SKEW_MIN),
            min_side=float(getattr(args, "min_side", lib.DEFAULT_MIN_SIDE) or lib.DEFAULT_MIN_SIDE),
            pm_stale_hours=float(
                getattr(args, "pm_stale_hours", lib.DEFAULT_PM_STALE_HOURS)
                if getattr(args, "pm_stale_hours", None) is not None
                else lib.DEFAULT_PM_STALE_HOURS
            ),
        )
    return _RUNTIME


def cmd_once(args: argparse.Namespace) -> int:
    rt = get_runtime(args)
    try:
        payload = rt.run_once(refresh=not args.offline)
    except Exception as e:  # noqa: BLE001
        print(f"bridge once failed: {e}", file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    path = root() / "data" / "bridge" / "matches.json"
    payload = lib.load_json(path, None)
    if not payload:
        print("no bridge snapshot yet; run: bridge_match.py once", file=sys.stderr)
        return 1
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    rt = get_runtime(args)
    # Merge file snapshot counts if runtime is cold.
    st = rt.status()
    snap = lib.load_json(root() / "data" / "bridge" / "matches.json", None)
    if snap and not st.get("last_result"):
        st["last_result"] = {
            "matched_at": snap.get("matched_at"),
            "count": snap.get("count"),
            "dqd_count": snap.get("dqd_count"),
            "pm_count": snap.get("pm_count"),
        }
    json.dump(st, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    rt = get_runtime(args)
    result = rt.start()
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    if args.foreground:
        print(
            f"match-bridge running (dqd={rt.dqd_interval}/{rt.dqd_idle_interval}s, "
            f"pm={rt.pm_interval}s). Ctrl+C to stop.",
            file=sys.stderr,
            flush=True,
        )

        def _stop(_sig: int, _frame: object) -> None:
            rt.stop()
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        while rt.running:
            time.sleep(1)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    rt = get_runtime(args)
    result = rt.stop()
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bridge Dongqiudi + Polymarket match lists")
    p.add_argument("--tab", default="full", help="Dongqiudi tab (default: full)")
    p.add_argument("--dqd-interval", type=int, default=15, help="DQD live poll seconds")
    p.add_argument("--dqd-idle-interval", type=int, default=60, help="DQD idle poll seconds")
    p.add_argument("--pm-interval", type=int, default=600, help="Polymarket refresh seconds (default 10m)")
    p.add_argument("--within-hours", type=int, default=48, help="PM upcoming window hours")
    p.add_argument(
        "--min-score",
        type=float,
        default=lib.DEFAULT_MIN_SCORE,
        help=f"Fuzzy match threshold 0-1 (default {lib.DEFAULT_MIN_SCORE})",
    )
    p.add_argument(
        "--min-side",
        type=float,
        default=lib.DEFAULT_MIN_SIDE,
        help=f"Min home AND away team similarity (default {lib.DEFAULT_MIN_SIDE})",
    )
    p.add_argument(
        "--max-skew-min",
        type=int,
        default=lib.DEFAULT_MAX_SKEW_MIN,
        help=f"Max absolute kickoff skew minutes (default {lib.DEFAULT_MAX_SKEW_MIN})",
    )
    p.add_argument(
        "--pm-stale-hours",
        type=float,
        default=lib.DEFAULT_PM_STALE_HOURS,
        help=f"Drop PM events with kickoff older than this many hours (default {lib.DEFAULT_PM_STALE_HOURS})",
    )
    sub = p.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="Refresh both skills once and emit matches JSON")
    once.add_argument("--offline", action="store_true", help="Rematch from existing snapshots only")
    once.add_argument("--json", action="store_true", default=True)
    once.set_defaults(func=cmd_once)

    lst = sub.add_parser("list", help="Print last bridge snapshot")
    lst.add_argument("--json", action="store_true", default=True)
    lst.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="Runtime / last match summary")
    st.add_argument("--json", action="store_true", default=True)
    st.set_defaults(func=cmd_status)

    start = sub.add_parser("start", help="Start DQD+PM loops at default cadences")
    start.add_argument(
        "--foreground",
        action="store_true",
        help="Block until Ctrl+C (daemon threads otherwise exit with process)",
    )
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="Stop background loops in this process")
    stop.set_defaults(func=cmd_stop)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

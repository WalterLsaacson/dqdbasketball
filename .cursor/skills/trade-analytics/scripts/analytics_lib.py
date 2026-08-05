#!/usr/bin/env python3
"""Historical trade analytics over polymarket-quote ledgers."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TZ_CN = timezone(timedelta(hours=8))

DEFAULT_TRADES = Path("data/pm-quote/trades.jsonl")
DEFAULT_OPENS = Path("data/pm-quote/open_positions.json")
DEFAULT_OPPS = Path("data/pm-quote/opportunities.jsonl")


def repo_root() -> Path:
    # scripts → trade-analytics → skills → .cursor → repo
    return Path(__file__).resolve().parents[4]


def data_dir(root: Path | None = None) -> Path:
    r = root or repo_root()
    d = r / "data" / "trade-analytics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def now_cn_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    return dt.astimezone(TZ_CN)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def trade_ts(row: dict[str, Any]) -> datetime | None:
    return parse_ts(row.get("quoted_at") or row.get("ts") or row.get("at"))


def filter_window(
    rows: list[dict[str, Any]],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    ts_fn=trade_ts,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        ts = ts_fn(row)
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        out.append(row)
    return out


def resolve_window(
    *,
    since: str | None = None,
    until: str | None = None,
    last_hours: float | None = None,
    last_days: float | None = None,
) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(TZ_CN)
    end = parse_ts(until) if until else None
    start = parse_ts(since) if since else None
    if last_hours is not None:
        start = now - timedelta(hours=float(last_hours))
    if last_days is not None:
        start = now - timedelta(days=float(last_days))
    return start, end


def plan_of(row: dict[str, Any]) -> dict[str, Any]:
    return as_dict(row.get("plan"))


def est_buy_win_profit(row: dict[str, Any]) -> float | None:
    """Assume buy WIN token redeems at 1.0 → shares * (1 - fill_price)."""
    if row.get("trade") != "buy_win":
        return None
    if row.get("status") not in ("dry_run", "posted", "live", "filled"):
        return None
    plan = plan_of(row)
    shares = plan.get("shares")
    levels = plan.get("levels") or []
    px = plan.get("worst_price") or plan.get("avg_price") or plan.get("price")
    if px is None and levels and isinstance(levels[0], dict):
        px = levels[0].get("price")
    if shares is None or px is None:
        return None
    try:
        return float(shares) * (1.0 - float(px))
    except (TypeError, ValueError):
        return None


def usdc_of(row: dict[str, Any]) -> float | None:
    plan = plan_of(row)
    for key in ("usdc", "cost_usdc", "size_usdc"):
        if plan.get(key) is not None:
            try:
                return float(plan[key])
            except (TypeError, ValueError):
                pass
    if row.get("usdc") is not None:
        try:
            return float(row["usdc"])
        except (TypeError, ValueError):
            return None
    return None


def compact_trade(row: dict[str, Any]) -> dict[str, Any]:
    plan = plan_of(row)
    return {
        "quoted_at": row.get("quoted_at"),
        "status": row.get("status"),
        "trade": row.get("trade"),
        "live": row.get("live"),
        "success": row.get("success"),
        "match_id": row.get("match_id"),
        "home": row.get("home"),
        "away": row.get("away"),
        "score": f"{row.get('home_score')}-{row.get('away_score')}",
        "family": row.get("family"),
        "outcome": row.get("outcome"),
        "market_key": row.get("market_key"),
        "settlement": row.get("settlement"),
        "net_edge": row.get("net_edge"),
        "best_ask": row.get("best_ask"),
        "best_bid": row.get("best_bid"),
        "usdc": usdc_of(row),
        "shares": plan.get("shares"),
        "est_profit": est_buy_win_profit(row),
        "skip_reason": row.get("skip_reason") or plan.get("skip_reason"),
        "event_key": row.get("event_key"),
    }


def load_open_positions(path: Path) -> list[dict[str, Any]]:
    raw = load_json(path, {})
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        for key in ("positions", "lots", "open"):
            val = raw.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
    return []


def summarize_trades(
    trades: list[dict[str, Any]],
    *,
    opens: list[dict[str, Any]] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    windowed = filter_window(trades, since=since, until=until)
    statuses = Counter(str(t.get("status") or "?") for t in windowed)
    kinds = Counter(str(t.get("trade") or "?") for t in windowed)
    families = Counter(str(t.get("family") or "?") for t in windowed)
    skip_reasons = Counter(
        str(t.get("skip_reason") or "?")
        for t in windowed
        if t.get("status") == "skipped"
    )

    buy_wins = [
        t
        for t in windowed
        if t.get("trade") == "buy_win"
        and t.get("status") in ("dry_run", "posted", "live", "filled")
    ]
    flattens = [
        t
        for t in windowed
        if t.get("trade") == "flatten_reversal"
        or "flatten" in str(t.get("status") or "")
    ]
    sell_lose_skips = [
        t
        for t in windowed
        if t.get("trade") == "sell_lose" and t.get("status") == "skipped"
    ]

    # Pair flatten after buy on same match+family (approx).
    flattened_buys: list[dict[str, Any]] = []
    kept_buys: list[dict[str, Any]] = []
    for buy in buy_wins:
        bts = trade_ts(buy)
        hit = False
        for flat in flattens:
            if flat.get("match_id") != buy.get("match_id"):
                continue
            if buy.get("family") and flat.get("family") and flat.get("family") != buy.get(
                "family"
            ):
                continue
            fts = trade_ts(flat)
            if bts and fts and fts >= bts:
                hit = True
                break
            if not bts or not fts:
                # fallback: same match after
                if flat.get("quoted_at") and buy.get("quoted_at"):
                    if str(flat["quoted_at"]) >= str(buy["quoted_at"]):
                        hit = True
                        break
        (flattened_buys if hit else kept_buys).append(buy)

    def money(rows: list[dict[str, Any]]) -> dict[str, float]:
        usdc = 0.0
        est = 0.0
        for r in rows:
            u = usdc_of(r)
            e = est_buy_win_profit(r)
            if u is not None:
                usdc += u
            if e is not None:
                est += e
        return {"usdc": round(usdc, 4), "est_profit": round(est, 4), "count": len(rows)}

    buy_all = money(buy_wins)
    buy_kept = money(kept_buys)
    buy_flat = money(flattened_buys)

    by_match: dict[str, dict[str, Any]] = {}
    for t in buy_wins:
        label = f"{t.get('home')} vs {t.get('away')}"
        slot = by_match.setdefault(
            label,
            {"match_id": t.get("match_id"), "buys": 0, "usdc": 0.0, "est_profit": 0.0},
        )
        slot["buys"] += 1
        u = usdc_of(t)
        e = est_buy_win_profit(t)
        if u is not None:
            slot["usdc"] = round(slot["usdc"] + u, 4)
        if e is not None:
            slot["est_profit"] = round(slot["est_profit"] + e, 4)

    open_lots = opens or []
    open_status = Counter(str(x.get("status") or "?") for x in open_lots)
    open_usdc = 0.0
    for x in open_lots:
        if x.get("status") != "open":
            continue
        try:
            open_usdc += float(x.get("usdc") or 0)
        except (TypeError, ValueError):
            pass

    ts_list = [trade_ts(t) for t in windowed]
    ts_list = [t for t in ts_list if t is not None]
    return {
        "analyzed_at": now_cn_iso(),
        "window": {
            "since": since.isoformat(timespec="seconds") if since else None,
            "until": until.isoformat(timespec="seconds") if until else None,
        },
        "counts": {
            "trades": len(windowed),
            "by_status": dict(statuses),
            "by_trade": dict(kinds),
            "by_family": dict(families),
            "skip_reasons": dict(skip_reasons),
            "sell_lose_skipped": len(sell_lose_skips),
            "flattens": len(flattens),
        },
        "pnl": {
            "note": (
                "dry-run/posted buy_win est_profit assumes redeem@$1; "
                "flatten pairs remove those buys from kept"
            ),
            "buy_win_all": buy_all,
            "buy_win_kept_after_flatten": buy_kept,
            "buy_win_later_flattened": buy_flat,
            "flatten_count": len(flattens),
        },
        "by_match": dict(
            sorted(by_match.items(), key=lambda kv: kv[1]["est_profit"], reverse=True)
        ),
        "open_positions": {
            "total": len(open_lots),
            "by_status": dict(open_status),
            "open_usdc": round(open_usdc, 4),
        },
        "span": {
            "first": min(ts_list).isoformat(timespec="seconds") if ts_list else None,
            "last": max(ts_list).isoformat(timespec="seconds") if ts_list else None,
        },
        "samples": {
            "buy_wins": [compact_trade(t) for t in buy_wins[-10:]],
            "flattens": [compact_trade(t) for t in flattens[-10:]],
            "skips": [compact_trade(t) for t in windowed if t.get("status") == "skipped"][
                -10:
            ],
        },
    }


def format_summary_text(report: dict[str, Any]) -> str:
    pnl = report.get("pnl") or {}
    counts = report.get("counts") or {}
    window = report.get("window") or {}
    span = report.get("span") or {}
    opens = report.get("open_positions") or {}
    lines = [
        f"Trade analytics @ {report.get('analyzed_at')}",
        f"window since={window.get('since') or '—'} until={window.get('until') or '—'}",
        f"span {span.get('first') or '—'} → {span.get('last') or '—'}",
        f"trades={counts.get('trades')}  status={counts.get('by_status')}",
        f"kinds={counts.get('by_trade')}  families={counts.get('by_family')}",
    ]
    if counts.get("skip_reasons"):
        lines.append(f"skips={counts.get('skip_reasons')}")
    all_p = pnl.get("buy_win_all") or {}
    kept = pnl.get("buy_win_kept_after_flatten") or {}
    flat = pnl.get("buy_win_later_flattened") or {}
    lines.append(
        f"buy_win: {all_p.get('count', 0)} fills · "
        f"${all_p.get('usdc', 0):.2f} · est +${all_p.get('est_profit', 0):.2f}"
    )
    lines.append(
        f"  kept after flatten: {kept.get('count', 0)} · "
        f"est +${kept.get('est_profit', 0):.2f}"
    )
    lines.append(
        f"  later flattened: {flat.get('count', 0)} · "
        f"est was +${flat.get('est_profit', 0):.2f} "
        f"(flatten events={pnl.get('flatten_count', 0)})"
    )
    lines.append(
        f"open_positions: {opens.get('total')} {opens.get('by_status')} "
        f"open_usdc=${opens.get('open_usdc', 0):.2f}"
    )
    by_match = report.get("by_match") or {}
    if by_match:
        lines.append("top matches by est_profit:")
        for i, (name, slot) in enumerate(by_match.items()):
            if i >= 8:
                break
            lines.append(
                f"  {slot.get('est_profit', 0):+.3f}  {name} "
                f"(buys={slot.get('buys')} usdc={slot.get('usdc')})"
            )
    return "\n".join(lines)


def build_report(
    root: Path | None = None,
    *,
    trades_path: Path | None = None,
    opens_path: Path | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    root = root or repo_root()
    tpath = trades_path or (root / DEFAULT_TRADES)
    opath = opens_path or (root / DEFAULT_OPENS)
    trades = load_jsonl(tpath)
    opens = load_open_positions(opath)
    report = summarize_trades(trades, opens=opens, since=since, until=until)
    report["sources"] = {
        "trades": str(tpath),
        "open_positions": str(opath),
        "trades_loaded": len(trades),
        "opens_loaded": len(opens),
    }
    if persist:
        out = data_dir(root) / "latest.json"
        write_json(out, report)
        report["report_path"] = str(out)
    return report

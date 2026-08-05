#!/usr/bin/env python3
"""Rolling retention for pm-quote / bridge runtime artifacts (default 24h)."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import quote_lib as lib
from market_cache import MarketCatalogCache, _match_finished_row

logger = logging.getLogger("pm_quote.data_prune")
TZ_CN = timezone(timedelta(hours=8))
DEFAULT_RETAIN_HOURS = 24.0


def _now_cn() -> datetime:
    return datetime.now(TZ_CN)


def lock_path_for(path: Path) -> Path:
    """Sidecar lock file shared by appenders and pruners (cross-process)."""
    return Path(str(path) + ".lock")


@contextmanager
def exclusive_jsonl_lock(path: Path) -> Iterator[None]:
    """Exclusive flock on ``{path}.lock`` for the duration of the block."""
    lock_path = lock_path_for(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _row_time(
    row: dict[str, Any], ts_keys: tuple[str, ...]
) -> datetime | None:
    for k in ts_keys:
        v = row.get(k)
        if not isinstance(v, str) or not v:
            continue
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_CN)
        return dt.astimezone(TZ_CN)
    return None


def prune_jsonl(
    path: Path,
    *,
    cutoff: datetime,
    ts_keys: tuple[str, ...] = ("quoted_at", "ts", "timestamp"),
    keep_row: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, int]:
    """Keep lines with timestamp >= cutoff (rolling window). Rewrite under flock.

    Lines that fail JSON parse are dropped. Lines with no parseable timestamp
    are kept. If ``keep_row`` returns True, the row is kept even when older than
    cutoff (used so unprocessed bridge events are never deleted).
    """
    if not path.is_file():
        return {"kept": 0, "removed": 0, "bytes_before": 0, "bytes_after": 0}

    with exclusive_jsonl_lock(path):
        if not path.is_file():
            return {"kept": 0, "removed": 0, "bytes_before": 0, "bytes_after": 0}
        before = path.stat().st_size
        kept = removed = 0
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            with path.open("r", encoding="utf-8") as fin, tmp.open(
                "w", encoding="utf-8"
            ) as fout:
                for line in fin:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except json.JSONDecodeError:
                        removed += 1
                        continue
                    if not isinstance(row, dict):
                        removed += 1
                        continue
                    if keep_row is not None and keep_row(row):
                        fout.write(json.dumps(row, ensure_ascii=False) + "\n")
                        kept += 1
                        continue
                    ts = _row_time(row, ts_keys)
                    if ts is not None and ts < cutoff:
                        removed += 1
                        continue
                    fout.write(json.dumps(row, ensure_ascii=False) + "\n")
                    kept += 1
            if removed == 0:
                tmp.unlink(missing_ok=True)
                return {
                    "kept": kept,
                    "removed": 0,
                    "bytes_before": before,
                    "bytes_after": before,
                }
            tmp.replace(path)
            after = path.stat().st_size if path.is_file() else 0
            return {
                "kept": kept,
                "removed": removed,
                "bytes_before": before,
                "bytes_after": after,
            }
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


def prune_market_cache(cache: MarketCatalogCache) -> dict[str, int | str]:
    """Drop finished / orphan catalog files not needed for open paired fixtures.

    Skips entirely when matches.json is missing, malformed, or has zero rows so
    a transient empty snapshot cannot wipe the whole cache.
    """
    matches_path = lib.bridge_dir(cache.root) / "matches.json"
    if not matches_path.is_file():
        return {
            "scanned": 0,
            "dropped": 0,
            "active_open": 0,
            "skipped": "no_matches_file",
        }
    snap = lib.load_json(matches_path, None)
    if not isinstance(snap, dict) or "matches" not in snap:
        return {
            "scanned": 0,
            "dropped": 0,
            "active_open": 0,
            "skipped": "bad_matches",
        }
    rows = list(snap.get("matches") or [])
    if not rows:
        return {
            "scanned": 0,
            "dropped": 0,
            "active_open": 0,
            "skipped": "empty_matches",
        }

    active_open: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        dqd = row.get("dongqiudi") or {}
        mid = str(dqd.get("id") or "")
        if not mid:
            continue
        if _match_finished_row(row):
            continue
        pm_h = row.get("polymarket") or {}
        if pm_h.get("event_id") or pm_h.get("slug"):
            active_open.add(mid)

    dropped = 0
    scanned = 0
    for path in list(cache.dir.glob("*.json")):
        scanned += 1
        mid = path.stem
        if mid in active_open:
            continue
        cache.drop(mid)
        dropped += 1
    return {"scanned": scanned, "dropped": dropped, "active_open": len(active_open)}


def prune_runtime_data(
    root: Path,
    *,
    retain_hours: float = DEFAULT_RETAIN_HOURS,
    market_cache: MarketCatalogCache | None = None,
) -> dict[str, Any]:
    """Prune market_cache + rolling 24h jsonl under pm-quote / bridge."""
    hours = max(1.0, float(retain_hours))
    cutoff = _now_cn() - timedelta(hours=hours)
    cache = market_cache or MarketCatalogCache(root)
    processed = set((lib.load_cursor(root).get("processed_keys") or []))
    report: dict[str, Any] = {
        "pruned_at": _now_cn().isoformat(timespec="seconds"),
        "retain_hours": hours,
        "cutoff": cutoff.isoformat(timespec="seconds"),
        "market_cache": prune_market_cache(cache),
        "jsonl": {},
    }

    def _keep_unprocessed_bridge(row: dict[str, Any]) -> bool:
        if row.get("type") not in ("score_change", "match_finished"):
            return False
        return lib.event_key(row) not in processed

    targets: list[tuple[Path, tuple[str, ...], Callable[[dict[str, Any]], bool] | None, str]] = [
        (lib.data_dir(root) / "quotes.jsonl", ("quoted_at", "ts"), None, "quotes.jsonl"),
        (
            lib.data_dir(root) / "opportunities.jsonl",
            ("quoted_at", "ts"),
            None,
            "opportunities.jsonl",
        ),
        (lib.data_dir(root) / "trades.jsonl", ("quoted_at", "ts"), None, "trades.jsonl"),
        (
            lib.data_dir(root) / "post_goal_samples.jsonl",
            ("sampled_at", "quoted_at", "ts"),
            None,
            "post_goal_samples.jsonl",
        ),
        (
            lib.bridge_dir(root) / "events.jsonl",
            ("ts", "quoted_at"),
            _keep_unprocessed_bridge,
            "bridge/events.jsonl",
        ),
    ]
    for path, keys, keep_row, label in targets:
        try:
            report["jsonl"][label] = prune_jsonl(
                path, cutoff=cutoff, ts_keys=keys, keep_row=keep_row
            )
        except Exception as e:  # noqa: BLE001
            report["jsonl"][label] = {"error": str(e)}
            logger.exception("prune failed for %s", path)
    return report


def start_pruner(
    root: Path,
    *,
    market_cache: MarketCatalogCache | None = None,
    retain_hours: float = DEFAULT_RETAIN_HOURS,
    interval_s: float = 600.0,
    stop_event: threading.Event | None = None,
    run_immediately: bool = True,
) -> threading.Thread:
    """Background thread: prune every ``interval_s`` (default 10 min)."""
    stop = stop_event or threading.Event()
    cache = market_cache or MarketCatalogCache(root)

    def _loop() -> None:
        first = True
        while not stop.is_set():
            if first and not run_immediately:
                first = False
                stop.wait(max(30.0, float(interval_s)))
                continue
            first = False
            try:
                report = prune_runtime_data(
                    root, retain_hours=retain_hours, market_cache=cache
                )
                mc = report.get("market_cache") or {}
                jl = report.get("jsonl") or {}
                removed = sum(
                    int((v or {}).get("removed") or 0)
                    for v in jl.values()
                    if isinstance(v, dict)
                )
                if mc.get("dropped") or mc.get("skipped") or removed:
                    logger.info("data_prune %s", report)
                    skip = f" skip={mc.get('skipped')}" if mc.get("skipped") else ""
                    print(
                        f"data_prune retain={retain_hours}h cache_dropped={mc.get('dropped')} "
                        f"jsonl_removed={removed}{skip}",
                        flush=True,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("data_prune failed")
            stop.wait(max(60.0, float(interval_s)))

    t = threading.Thread(target=_loop, name="pm-quote-data-prune", daemon=True)
    t.start()
    return t

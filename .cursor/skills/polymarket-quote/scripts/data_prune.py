#!/usr/bin/env python3
"""Rolling retention for pm-quote / bridge runtime artifacts (default 24h)."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import quote_lib as lib
from market_cache import MarketCatalogCache, _match_finished_row

logger = logging.getLogger("pm_quote.data_prune")
TZ_CN = timezone(timedelta(hours=8))
DEFAULT_RETAIN_HOURS = 24.0
# Bridge event log is pruned by consumer byte offset (not by wall clock), so a
# live file stays bounded even when the tail is older than the retain window.
# Leftover ``events.jsonl.<random>.tmp`` from an interrupted rewrite is removed
# once it is older than this.
ORPHAN_TMP_MIN_AGE_S = 3600.0
# Skip the bridge rewrite unless at least this much consumed prefix can go.
BRIDGE_PRUNE_MIN_BYTES = 32 * 1024 * 1024
# Rotate the bridge log once it grows past this (consumers follow the offset).
BRIDGE_ROTATE_BYTES = 200 * 1024 * 1024
# Rotated ``events.N.jsonl`` archives are history only; keep the newest few.
BRIDGE_ROTATE_KEEP = 2


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


def orphan_tmp_files(path: Path) -> list[Path]:
    """Temp files that an interrupted ``prune_jsonl`` left behind."""
    try:
        return sorted(path.parent.glob(f"{path.name}.*.tmp"))
    except OSError:
        return []


def cleanup_orphan_tmp(
    path: Path,
    *,
    min_age_s: float = ORPHAN_TMP_MIN_AGE_S,
    now: float | None = None,
    in_use: set[str] | None = None,
) -> dict[str, int]:
    """Unlink stale ``{path}.<random>.tmp`` leftovers from crashed prunes.

    ``min_age_s`` keeps the temp file of an in-flight rewrite: that file is
    created seconds ago. ``in_use`` (absolute paths) is an extra guard for
    callers that know a temp file is actively being written.
    """
    removed = freed = 0
    if not path.parent.is_dir():
        return {"removed": 0, "freed_bytes": 0}
    cutoff = (time.time() if now is None else float(now)) - max(0.0, float(min_age_s))
    busy = {str(Path(x).resolve()) for x in (in_use or set())}
    for tmp in orphan_tmp_files(path):
        try:
            st = tmp.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff:
            continue
        if str(tmp.resolve()) in busy:
            continue
        try:
            tmp.unlink()
        except OSError:
            continue
        removed += 1
        freed += int(st.st_size)
    if removed:
        logger.info(
            "removed %d orphan prune temp file(s) for %s (%.2f MB freed)",
            removed,
            path.name,
            freed / 1024**2,
        )
    return {"removed": removed, "freed_bytes": freed}


def _scan_back_to_line_start(f, size: int) -> int:
    """Offset just past the last newline in ``f`` (0 when there is none)."""
    back = size
    step = 64 * 1024
    while back > 0:
        start = max(0, back - step)
        f.seek(start)
        nl = f.read(back - start).rfind(b"\n")
        if nl >= 0:
            return start + nl + 1
        back = start
    return 0


def _line_start_at_or_before(path: Path, offset: int) -> int:
    """Return the offset of the first byte of the line containing ``offset``.

    Used to keep a byte cursor line-aligned. Clamps into ``[0, size]``.
    """
    size = path.stat().st_size
    pos = max(0, min(int(offset), size))
    if pos == 0:
        return 0
    if pos >= size:
        # A cursor at EOF is only line-aligned when the file ends with a
        # newline; otherwise the writer is mid-line and those bytes are unread.
        with path.open("rb") as f:
            if size:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    # Only the complete lines before this partial one are
                    # safe to drop.
                    return _scan_back_to_line_start(f, size)
        return size
    with path.open("rb") as f:
        f.seek(pos - 1)
        if f.read(1) == b"\n":
            return pos
        return _scan_back_to_line_start(f, pos)


def _archive_files(path: Path) -> list[tuple[int, Path]]:
    """``events.N.jsonl`` siblings of ``path`` as ``(n, path)`` sorted by n."""
    out: list[tuple[int, Path]] = []
    prefix = f"{path.stem}."
    for cand in path.parent.glob(f"{path.stem}.*{path.suffix}"):
        tail = cand.name[len(prefix): -len(path.suffix)]
        if tail.isdigit():
            out.append((int(tail), cand))
    return sorted(out)


def _next_archive_path(path: Path) -> Path:
    """Next unused ``events.N.jsonl``; numbers only grow so name order == age."""
    existing = _archive_files(path)
    n = (existing[-1][0] + 1) if existing else 1
    return path.with_name(f"{path.stem}.{n}{path.suffix}")


def prune_bridge_archives(path: Path, keep: int) -> dict[str, int]:
    """Keep only the newest ``keep`` rotated bridge logs; unlink the rest."""
    removed = freed = 0
    if keep < 0:
        return {"removed": 0, "freed_bytes": 0}
    archives = _archive_files(path)
    for _, cand in archives[: max(0, len(archives) - int(keep))]:
        try:
            size = cand.stat().st_size
            cand.unlink()
        except OSError:
            continue
        removed += 1
        freed += int(size)
    if removed:
        logger.info(
            "removed %d rotated bridge log(s) (%.1f MB freed, keeping %d)",
            removed,
            freed / 1024**2,
            keep,
        )
    return {"removed": removed, "freed_bytes": freed}


def prune_bridge_consumed(
    root: Path,
    *,
    min_bytes: int = BRIDGE_PRUNE_MIN_BYTES,
    rotate_threshold: int | None = BRIDGE_ROTATE_BYTES,
    rotate_keep: int = BRIDGE_ROTATE_KEEP,
) -> dict[str, Any]:
    """Drop (or archive) the bridge log prefix already consumed by the quote loop.

    Must be called by the quote consumer itself, right after it saves its
    cursor: only that reader knows which bytes are safe to discard, and doing
    it from a side thread would race with the consumer writing back a stale
    absolute offset.

    The cursor is rebased by the number of bytes removed, so the consumer's
    next read continues at exactly the same logical event.
    """
    path = lib.bridge_dir(root) / "events.jsonl"
    out: dict[str, Any] = {"pruned": False}
    if not path.is_file():
        return out
    with exclusive_jsonl_lock(path):
        if not path.is_file():
            return out
        cursor = lib.load_cursor(root)
        offset = int(cursor.get("events_byte_offset") or 0)
        size = path.stat().st_size
        if offset <= 0:
            return {**out, "size": size, "reason": "no_cursor"}
        if offset > size:
            # Cursor past EOF means the file was replaced behind our back.
            # Trimming here would delete unread rows, so leave it alone.
            return {**out, "size": size, "reason": "cursor_beyond_eof"}
        start = _line_start_at_or_before(path, offset)
        dq = start
        if dq <= 0:
            return {**out, "size": size, "reason": "nothing_consumed"}
        rotate = rotate_threshold is not None and size >= int(rotate_threshold)
        if not rotate and dq < max(0, int(min_bytes)):
            return {**out, "size": size, "reason": "below_min_bytes", "prefix": dq}

        fd_tail, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        tmp_tail = Path(tmp_name)
        archive: Path | None = None
        try:
            farch = None
            if rotate:
                archive = _next_archive_path(path.parent / path.name)
                farch = archive.open("wb")
            with path.open("rb") as fin, os.fdopen(fd_tail, "wb") as ftail:
                if farch is not None:
                    left = start
                    while left > 0:
                        buf = fin.read(min(4 * 1024 * 1024, left))
                        if not buf:
                            break
                        farch.write(buf)
                        left -= len(buf)
                    farch.flush()
                    os.fsync(farch.fileno())
                else:
                    fin.seek(start)
                while True:
                    buf = fin.read(4 * 1024 * 1024)
                    if not buf:
                        break
                    ftail.write(buf)
                ftail.flush()
                os.fsync(ftail.fileno())
            if farch is not None:
                farch.close()
            tmp_tail.replace(path)
        except Exception:
            tmp_tail.unlink(missing_ok=True)
            if archive is not None and archive.exists():
                archive.unlink(missing_ok=True)
            raise

        live = path.stat().st_size if path.is_file() else 0
        cursor["events_byte_offset"] = max(0, offset - start)
        cursor["updated_at"] = _now_cn().isoformat(timespec="seconds")
        lib.save_cursor(root, cursor)
    if rotate:
        logger.info(
            "rotated bridge log: %s (archived %.1f MB, live %.1f MB)",
            archive.name if archive else "?",
            start / 1024**2,
            live / 1024**2,
        )
        prune_bridge_archives(path, rotate_keep)
    else:
        logger.info(
            "pruned bridge log prefix: %.1f MB dropped, %.1f MB live",
            start / 1024**2,
            live / 1024**2,
        )
    return {
        "pruned": True,
        "rotated": bool(rotate),
        "archive": str(archive) if archive else "",
        "removed_bytes": int(start),
        "live_bytes": int(live),
        "cursor_offset": int(cursor["events_byte_offset"]),
    }


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

    The scan is read-only: the temp file is created lazily at the first
    removable line, and its prefix is copied byte-for-byte from the source. A
    file with nothing to drop therefore costs one read pass and zero writes.
    """
    empty = {"kept": 0, "removed": 0, "bytes_before": 0, "bytes_after": 0}
    if not path.is_file():
        return empty

    with exclusive_jsonl_lock(path):
        if not path.is_file():
            return empty
        before = path.stat().st_size
        kept = removed = 0
        tmp: Path | None = None
        ftail = None
        try:
            with path.open("rb") as fin:
                while True:
                    line_start = fin.tell()
                    line = fin.readline()
                    if not line:
                        break
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        row = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        row = None
                    if not isinstance(row, dict):
                        keep_line = False
                    elif keep_row is not None and keep_row(row):
                        keep_line = True
                    else:
                        ts = _row_time(row, ts_keys)
                        keep_line = ts is None or ts >= cutoff
                    if keep_line:
                        kept += 1
                        if ftail is not None:
                            ftail.write(line)
                        continue
                    removed += 1
                    if ftail is None:
                        fd, tmp_name = tempfile.mkstemp(
                            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
                        )
                        tmp = Path(tmp_name)
                        ftail = os.fdopen(fd, "wb")
                        # Copy the unchanged prefix verbatim instead of
                        # re-serialising every retained row.
                        with path.open("rb") as fsrc:
                            left = line_start
                            while left > 0:
                                buf = fsrc.read(min(4 * 1024 * 1024, left))
                                if not buf:
                                    break
                                ftail.write(buf)
                                left -= len(buf)
            if removed == 0 or tmp is None:
                if ftail is not None:
                    ftail.close()
                    ftail = None
                    tmp.unlink(missing_ok=True)
                    tmp = None
                return {
                    "kept": kept,
                    "removed": 0,
                    "bytes_before": before,
                    "bytes_after": before,
                }
            ftail.flush()
            os.fsync(ftail.fileno())
            ftail.close()
            ftail = None
            tmp.replace(path)
            after = path.stat().st_size if path.is_file() else 0
            return {
                "kept": kept,
                "removed": removed,
                "bytes_before": before,
                "bytes_after": after,
            }
        except Exception:
            if ftail is not None:
                ftail.close()
            if tmp is not None:
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
    report: dict[str, Any] = {
        "pruned_at": _now_cn().isoformat(timespec="seconds"),
        "retain_hours": hours,
        "cutoff": cutoff.isoformat(timespec="seconds"),
        "market_cache": prune_market_cache(cache),
        "jsonl": {},
    }

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
    ]
    orphan_freed = 0
    orphan_removed = 0
    for path, keys, keep_row, label in targets:
        try:
            # Reclaim temp files left by an interrupted rewrite before scanning
            # the live file: those can be hundreds of MB each and cost nothing
            # to drop once they are past the in-flight age window.
            orphans = cleanup_orphan_tmp(path)
            orphan_removed += orphans.get("removed", 0)
            orphan_freed += orphans.get("freed_bytes", 0)
            report["jsonl"][label] = prune_jsonl(
                path, cutoff=cutoff, ts_keys=keys, keep_row=keep_row
            )
        except Exception as e:  # noqa: BLE001
            report["jsonl"][label] = {"error": str(e)}
            logger.exception("prune failed for %s", path)

    # The bridge log is NOT rewritten here. Its consumer owns
    # ``events_byte_offset`` and rebases it after trimming (see
    # ``prune_bridge_consumed``); a side thread trimming the file would race
    # with the consumer writing back a stale absolute offset.
    bridge_path = lib.bridge_dir(root) / "events.jsonl"

    # Leftover temp files from a crashed rewrite (reclaims GBs after a crash).
    try:
        bridge_orphans = cleanup_orphan_tmp(bridge_path)
        orphan_removed += bridge_orphans.get("removed", 0)
        orphan_freed += bridge_orphans.get("freed_bytes", 0)
    except Exception as e:  # noqa: BLE001
        bridge_orphans = {"error": str(e)}
        logger.exception("orphan tmp cleanup failed for %s", bridge_path)
    report["orphan_tmp"] = {
        **bridge_orphans,
        "removed_total": orphan_removed,
        "freed_bytes_total": orphan_freed,
    }
    return report


def start_pruner(
    root: Path,
    *,
    market_cache: MarketCatalogCache | None = None,
    retain_hours: float = DEFAULT_RETAIN_HOURS,
    interval_s: float = 3600.0,
    stop_event: threading.Event | None = None,
    run_immediately: bool = True,
) -> threading.Thread:
    """Background thread: prune every ``interval_s`` (default 60 min)."""
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

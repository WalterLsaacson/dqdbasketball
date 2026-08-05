#!/usr/bin/env python3
"""Gamma market catalog cache: warm after bridge match, drop on FT."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import quote_lib as lib

logger = logging.getLogger("pm_quote.market_cache")
TZ_CN = timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def _match_finished_row(row: dict[str, Any]) -> bool:
    dqd = row.get("dongqiudi") or {}
    if row.get("finished") or dqd.get("is_finished"):
        return True
    st = str(dqd.get("status") or dqd.get("status_raw") or "").lower()
    return "played" in st or st in ("finished", "ft")


def catalog_complete(hit: dict[str, Any] | None) -> bool:
    """True when main Gamma event is present and related siblings were fully resolved."""
    if not isinstance(hit, dict):
        return False
    return bool(hit.get("related_complete")) and isinstance(hit.get("main_event"), dict)


class MarketCatalogCache:
    """In-memory + disk cache of Gamma main / more-markets / exact-score events."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = lib.data_dir(self.root) / "market_cache"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._mem: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def _path(self, match_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(match_id))
        return self.dir / f"{safe}.json"

    def get(self, match_id: str) -> dict[str, Any] | None:
        mid = str(match_id or "")
        if not mid:
            return None
        with self._lock:
            if mid in self._mem:
                return self._mem[mid]
            path = self._path(mid)
            if not path.is_file():
                return None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if isinstance(raw, dict):
                self._mem[mid] = raw
                return raw
            return None

    def put(self, match_id: str, payload: dict[str, Any]) -> None:
        mid = str(match_id or "")
        if not mid:
            return
        row = dict(payload)
        row["match_id"] = mid
        row["warmed_at"] = row.get("warmed_at") or _now_iso()
        with self._lock:
            self._mem[mid] = row
            path = self._path(mid)
            path.write_text(
                json.dumps(row, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def drop(self, match_id: str) -> None:
        mid = str(match_id or "")
        if not mid:
            return
        with self._lock:
            self._mem.pop(mid, None)
            path = self._path(mid)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def warm_match(
        self,
        row: dict[str, Any],
        *,
        proxy: str | None | object = ...,
        force: bool = False,
    ) -> dict[str, Any] | None:
        """Fetch Gamma main + related events for a paired bridge match row."""
        dqd = row.get("dongqiudi") or {}
        pm_h = row.get("polymarket") or {}
        mid = str(dqd.get("id") or row.get("match_id") or "")
        if not mid:
            return None
        if not force:
            hit = self.get(mid)
            if catalog_complete(hit):
                return hit
        if _match_finished_row(row):
            self.drop(mid)
            return None

        event_id = str(pm_h.get("event_id") or "") or None
        slug = str(pm_h.get("slug") or "") or None
        home = str(pm_h.get("home") or dqd.get("home") or "")
        away = str(pm_h.get("away") or dqd.get("away") or "")
        if not event_id and not slug:
            return None

        prev = self.get(mid) or {}
        try:
            main = lib.fetch_gamma_event(event_id=event_id, slug=slug, proxy=proxy)
        except Exception as e:  # noqa: BLE001
            logger.warning("warm main failed match=%s: %s", mid, e)
            main = prev.get("main_event") if isinstance(prev.get("main_event"), dict) else None

        related: dict[str, Any] = {
            "more_markets": prev.get("more_markets"),
            "exact_score": prev.get("exact_score"),
        }
        related_ok = bool(prev.get("related_complete"))
        if slug:
            try:
                related = lib.discover_related_events(
                    slug=slug, home=home, away=away, proxy=proxy
                )
                related_ok = True
            except Exception as e:  # noqa: BLE001
                logger.warning("warm related failed match=%s: %s", mid, e)
                related_ok = False
        else:
            related_ok = True

        payload = {
            "match_id": mid,
            "event_id": event_id or "",
            "slug": slug or "",
            "home": home,
            "away": away,
            "main_event": main,
            "more_markets": related.get("more_markets"),
            "exact_score": related.get("exact_score"),
            "related_complete": related_ok and isinstance(main, dict),
            "warmed_at": _now_iso(),
        }
        self.put(mid, payload)
        logger.info(
            "warmed catalog match=%s main=%s more=%s exact=%s complete=%s",
            mid,
            bool(main),
            bool(payload.get("more_markets")),
            bool(payload.get("exact_score")),
            payload.get("related_complete"),
        )
        return payload

    def sync_from_bridge_matches(
        self,
        *,
        proxy: str | None | object = ...,
        max_warms: int = 8,
    ) -> dict[str, int]:
        """Warm open matched fixtures; drop finished ones.

        Caps new Gamma warms per call so the background thread stays responsive
        while gradually filling ``market_cache/``.
        """
        warmed = dropped = skipped = 0
        budget = max(0, int(max_warms))
        for row in lib.load_bridge_matches(self.root):
            dqd = row.get("dongqiudi") or {}
            mid = str(dqd.get("id") or "")
            if not mid:
                skipped += 1
                continue
            if _match_finished_row(row):
                if self.get(mid) is not None:
                    self.drop(mid)
                    dropped += 1
                continue
            pm_h = row.get("polymarket") or {}
            if not (pm_h.get("event_id") or pm_h.get("slug")):
                skipped += 1
                continue
            before = self.get(mid)
            if catalog_complete(before):
                skipped += 1
                continue
            if budget <= 0:
                skipped += 1
                continue
            self.warm_match(row, proxy=proxy, force=not catalog_complete(before))
            budget -= 1
            after = self.get(mid)
            if catalog_complete(after) and not catalog_complete(before):
                warmed += 1
            elif after and not catalog_complete(before):
                warmed += 1  # progress / retry write
            else:
                skipped += 1
        return {"warmed": warmed, "dropped": dropped, "skipped": skipped}


def start_warmer(
    cache: MarketCatalogCache,
    *,
    proxy: str | None | object = ...,
    interval_s: float = 5.0,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Background thread: keep catalogs warm from matches.json."""
    stop = stop_event or threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                stats = cache.sync_from_bridge_matches(proxy=proxy)
                if stats.get("warmed") or stats.get("dropped"):
                    logger.info("market_cache sync %s", stats)
            except Exception:  # noqa: BLE001
                logger.exception("market_cache warmer failed")
            stop.wait(max(1.0, float(interval_s)))

    t = threading.Thread(target=_loop, name="pm-quote-market-cache", daemon=True)
    t.start()
    return t


def file_signature(path: Path) -> tuple[int, int]:
    """(mtime_ns, size) for event-driven wake; missing file → (0, 0)."""
    try:
        st = path.stat()
        return (getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)), int(st.st_size))
    except OSError:
        return (0, 0)


def wait_for_file_change(
    path: Path,
    prev: tuple[int, int],
    *,
    timeout_s: float,
    poll_s: float = 0.05,
) -> tuple[int, int]:
    """Wait until path signature changes or timeout; return latest signature."""
    deadline = time.time() + max(0.0, float(timeout_s))
    cur = file_signature(path)
    if cur != prev:
        return cur
    while time.time() < deadline:
        time.sleep(max(0.01, float(poll_s)))
        cur = file_signature(path)
        if cur != prev:
            return cur
    return file_signature(path)

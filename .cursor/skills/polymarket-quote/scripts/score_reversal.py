"""Score-reversal helpers + open buy_win ledger for emergency FAK flatten."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("pm_quote.reversal")

TZ_CN = timezone(timedelta(hours=8))

AF_STATUS_PENDING = "pending"
AF_STATUS_CONFIRMED = "confirmed"
AF_STATUS_NONE = "none"

FILL_STATUS_OPEN = "open"
FILL_STATUS_PENDING = "pending_fill"


def score_pair(home: Any, away: Any) -> tuple[int, int] | None:
    try:
        if home is None or away is None:
            return None
        return int(home), int(away)
    except (TypeError, ValueError):
        return None


def is_score_decrease(
    prev: tuple[int, int] | None,
    curr: tuple[int, int] | None,
) -> bool:
    if prev is None or curr is None:
        return False
    return curr[0] < prev[0] or curr[1] < prev[1]


def event_signals_reversal(ev: dict[str, Any]) -> bool:
    """True if bridge marked reversal or prev→curr score dropped."""
    if ev.get("is_reversal"):
        return True
    if (ev.get("type") or "") != "score_change":
        return False
    prev = ev.get("prev") or {}
    curr = ev.get("curr") or {}
    p = score_pair(prev.get("home"), prev.get("away"))
    c = score_pair(
        curr.get("home", ev.get("home_score")),
        curr.get("away", ev.get("away_score")),
    )
    return is_score_decrease(p, c)


def ft_reversal_vs_entry(
    *,
    entry: tuple[int, int] | None,
    ft: tuple[int, int] | None,
) -> bool:
    """FT/curr score lower than entry on either side → entry depended on a later-disallowed goal."""
    return is_score_decrease(entry, ft)


def entry_tuple(lot: dict[str, Any]) -> tuple[int, int] | None:
    entry = lot.get("entry_score")
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        return score_pair(entry[0], entry[1])
    return None


def lot_depends_on_disallowed_goal(
    lot: dict[str, Any],
    *,
    after_score: tuple[int, int] | None,
) -> bool:
    """True if this buy was opened at a score that is strictly 'higher' than after_score.

    Example: entry 1-2, after 1-1 → away goal blown → Over 2.5 lot depends on it.
    entry 1-0 Exact 0-0 No, after 1-1 → not a decrease vs entry → keep.
    """
    return is_score_decrease(entry_tuple(lot), after_score)


def iso_now() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    raw = str(ts).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_CN)
    return dt


def deadline_iso(timeout_s: float, *, now: datetime | None = None) -> str:
    base = now if now is not None else datetime.now(TZ_CN)
    return (base + timedelta(seconds=max(0.0, float(timeout_s)))).isoformat(
        timespec="seconds"
    )


def lot_af_pending(lot: dict[str, Any]) -> bool:
    return str(lot.get("af_status") or "") == AF_STATUS_PENDING


def lot_af_overdue(lot: dict[str, Any], *, now: datetime | None = None) -> bool:
    if not lot_af_pending(lot):
        return False
    dl = parse_iso(str(lot.get("af_deadline") or "") or None)
    if dl is None:
        return False
    cur = now if now is not None else datetime.now(TZ_CN)
    return cur >= dl


class OpenPositionLedger:
    """Persist open buy_win lots per match for reversal flatten."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._rows: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            self._rows = []
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._rows = []
            return
        if isinstance(raw, list):
            self._rows = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict) and isinstance(raw.get("positions"), list):
            self._rows = [r for r in raw["positions"] if isinstance(r, dict)]
        else:
            self._rows = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Keep only recent closed + all open to bound size
        open_rows = [r for r in self._rows if r.get("status") == "open"]
        closed = [r for r in self._rows if r.get("status") != "open"][-200:]
        payload = {"positions": open_rows + closed}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def register_buy(
        self,
        *,
        match_id: str,
        token_id: str,
        market_key: str,
        shares: float,
        usdc: float,
        home_score: Any,
        away_score: Any,
        live: bool,
        event_key: str,
        home: str = "",
        away: str = "",
        family: str = "",
        tick_size: str = "0.01",
        neg_risk: bool | None = None,
        af_status: str = AF_STATUS_NONE,
        af_deadline: str | None = None,
        fill_status: str = FILL_STATUS_OPEN,
    ) -> None:
        if not match_id or not token_id or shares <= 0:
            return
        sc = score_pair(home_score, away_score)
        status = str(af_status or AF_STATUS_NONE)
        if status not in (AF_STATUS_PENDING, AF_STATUS_CONFIRMED, AF_STATUS_NONE):
            status = AF_STATUS_NONE
        fill = str(fill_status or FILL_STATUS_OPEN)
        if fill not in (FILL_STATUS_OPEN, FILL_STATUS_PENDING):
            fill = FILL_STATUS_OPEN
        # Aggregate with prior open lot for same match+token (avoid under-selling).
        existing = None
        kept: list[dict[str, Any]] = []
        for r in self._rows:
            if (
                r.get("status") == "open"
                and str(r.get("match_id")) == str(match_id)
                and str(r.get("token_id")) == str(token_id)
            ):
                existing = r
            else:
                kept.append(r)
        self._rows = kept
        if existing is not None:
            shares = float(existing.get("shares") or 0) + float(shares)
            usdc = float(existing.get("usdc") or 0) + float(usdc)
            if existing.get("pending_flatten"):
                # Keep exit intent across top-ups.
                pass
            # Prefer confirmed AF status if either lot is confirmed.
            if existing.get("af_status") == AF_STATUS_CONFIRMED or status == AF_STATUS_CONFIRMED:
                status = AF_STATUS_CONFIRMED
                af_deadline = None
            elif existing.get("af_status") == AF_STATUS_PENDING and status == AF_STATUS_NONE:
                status = AF_STATUS_PENDING
                af_deadline = existing.get("af_deadline") or af_deadline
            if existing.get("fill_status") == FILL_STATUS_OPEN:
                fill = FILL_STATUS_OPEN
            event_key = str(existing.get("event_key") or event_key)
            if existing.get("entry_score"):
                sc = score_pair(
                    (existing.get("entry_score") or [None, None])[0],
                    (existing.get("entry_score") or [None, None])[1],
                ) or sc
        row = {
            "status": "open",
            "match_id": str(match_id),
            "token_id": str(token_id),
            "market_key": market_key or str(existing.get("market_key") if existing else ""),
            "family": family or str(existing.get("family") if existing else ""),
            "shares": float(shares),
            "usdc": float(usdc),
            "entry_score": list(sc) if sc else None,
            "home": home or str(existing.get("home") if existing else ""),
            "away": away or str(existing.get("away") if existing else ""),
            "live": bool(live if existing is None else (live or existing.get("live"))),
            "event_key": event_key,
            "tick_size": tick_size or "0.01",
            "neg_risk": neg_risk if neg_risk is not None else (existing.get("neg_risk") if existing else None),
            "af_status": status,
            "af_deadline": af_deadline if status == AF_STATUS_PENDING else None,
            "fill_status": fill,
        }
        if existing is not None and existing.get("pending_flatten"):
            row["pending_flatten"] = True
            row["pending_reason"] = existing.get("pending_reason")
        self._rows.append(row)
        self._save()
        logger.info(
            "ledger open match=%s token=%s… shares=%.4f entry=%s af=%s fill=%s",
            match_id,
            token_id[:12],
            shares,
            sc,
            status,
            fill,
        )

    def mark_fill_open(self, token_id: str, match_id: str) -> int:
        """Promote pending_fill → open after balance appears."""
        n = 0
        tid, mid = str(token_id), str(match_id)
        for r in self._rows:
            if (
                r.get("status") == "open"
                and str(r.get("token_id")) == tid
                and str(r.get("match_id")) == mid
                and r.get("fill_status") == FILL_STATUS_PENDING
            ):
                r["fill_status"] = FILL_STATUS_OPEN
                n += 1
        if n:
            self._save()
        return n

    def open_for_match(self, match_id: str) -> list[dict[str, Any]]:
        mid = str(match_id)
        return [
            r
            for r in self._rows
            if r.get("status") == "open" and str(r.get("match_id")) == mid
        ]

    def af_pending_lots(
        self,
        *,
        match_id: str | None = None,
        event_key: str | None = None,
    ) -> list[dict[str, Any]]:
        mid = str(match_id) if match_id else None
        ek = str(event_key) if event_key else None
        out: list[dict[str, Any]] = []
        for r in self._rows:
            if r.get("status") != "open" or not lot_af_pending(r):
                continue
            if mid is not None and str(r.get("match_id")) != mid:
                continue
            if ek is not None and str(r.get("event_key") or "") != ek:
                continue
            out.append(r)
        return out

    def overdue_af_pending_lots(
        self,
        *,
        now: datetime | None = None,
        exclude_event_keys: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        cur = now if now is not None else datetime.now(TZ_CN)
        skip = exclude_event_keys or set()
        return [
            r
            for r in self._rows
            if r.get("status") == "open"
            and lot_af_overdue(r, now=cur)
            and str(r.get("event_key") or "") not in skip
        ]

    def mark_af_confirmed(
        self,
        match_id: str,
        *,
        event_key: str = "",
    ) -> int:
        mid = str(match_id)
        ek = str(event_key or "")
        n = 0
        for r in self._rows:
            if r.get("status") != "open" or str(r.get("match_id")) != mid:
                continue
            if not lot_af_pending(r):
                continue
            if ek and str(r.get("event_key") or "") != ek:
                continue
            r["af_status"] = AF_STATUS_CONFIRMED
            r["af_deadline"] = None
            # Cancel any in-flight emergency flatten from a near-timeout race.
            r.pop("pending_flatten", None)
            r.pop("pending_reason", None)
            n += 1
        if n:
            self._save()
            logger.info(
                "ledger af_confirmed match=%s event_key=%s lots=%d",
                mid,
                ek or "*",
                n,
            )
        return n

    def refresh_af_deadline(
        self,
        match_id: str,
        *,
        event_key: str = "",
        timeout_s: float = 90.0,
    ) -> int:
        """Reset af_deadline for pending lots (align with AF submit clock)."""
        mid = str(match_id)
        ek = str(event_key or "")
        dl = deadline_iso(timeout_s)
        n = 0
        for r in self._rows:
            if r.get("status") != "open" or str(r.get("match_id")) != mid:
                continue
            if not lot_af_pending(r):
                continue
            if ek and str(r.get("event_key") or "") != ek:
                continue
            r["af_deadline"] = dl
            n += 1
        if n:
            self._save()
        return n

    def mark_closed(self, token_id: str, match_id: str, *, reason: str) -> None:
        mid = str(match_id)
        tid = str(token_id)
        changed = False
        for r in self._rows:
            if (
                r.get("status") == "open"
                and str(r.get("match_id")) == mid
                and str(r.get("token_id")) == tid
            ):
                r["status"] = "closed"
                r["close_reason"] = reason
                r.pop("pending_flatten", None)
                r.pop("pending_reason", None)
                r.pop("flatten_attempts", None)
                changed = True
        if changed:
            self._save()

    def mark_pending_flatten(
        self,
        token_id: str,
        match_id: str,
        *,
        reason: str,
    ) -> None:
        """Keep lot open but flag for retry on next watch tick."""
        mid = str(match_id)
        tid = str(token_id)
        changed = False
        for r in self._rows:
            if (
                r.get("status") == "open"
                and str(r.get("match_id")) == mid
                and str(r.get("token_id")) == tid
            ):
                r["pending_flatten"] = True
                r["pending_reason"] = str(reason or "")[:500]
                r["flatten_attempts"] = int(r.get("flatten_attempts") or 0) + 1
                changed = True
        if changed:
            self._save()

    def pending_flatten_lots(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self._rows
            if r.get("status") == "open" and r.get("pending_flatten")
        ]

    def purge_dry_run_opens(self, *, reason: str = "pre_live_purge") -> int:
        """Close simulated (live=False) open lots before enabling live trading."""
        return self.purge_dry_run_opens_for_signals(
            {"score_change", "match_finished"},
            reason=reason,
            purge_unknown=True,
        )

    def purge_dry_run_opens_for_signals(
        self,
        signals: set[str],
        *,
        reason: str = "pre_live_purge",
        purge_unknown: bool = False,
    ) -> int:
        """Close dry open lots whose event_key signal is in ``signals``.

        When both score_change and match_finished are live, pass both and
        ``purge_unknown=True`` to match the old full purge.
        """
        if not signals:
            return 0
        n = 0
        for r in self._rows:
            if r.get("status") != "open" or r.get("live"):
                continue
            ek = str(r.get("event_key") or "")
            sig = ek.split("|", 1)[0] if ek else ""
            if sig in signals or (purge_unknown and not sig):
                r["status"] = "closed"
                r["close_reason"] = reason
                r.pop("pending_flatten", None)
                n += 1
        if n:
            self._save()
        return n

    def all_open(self) -> list[dict[str, Any]]:
        return [r for r in self._rows if r.get("status") == "open"]

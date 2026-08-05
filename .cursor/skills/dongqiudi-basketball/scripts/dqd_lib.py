#!/usr/bin/env python3
"""Dongqiudi basketball match, boxscore, and event helpers."""

from __future__ import annotations

import json
import http.client
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

BASE = "https://www.dongqiudi.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

FLAG_HOT = 320

TZ_CN = timezone(timedelta(hours=8))

TAB_FILTERS: dict[str, Callable[[dict[str, Any]], bool]] = {}


class FetchError(RuntimeError):
    """Raised when the upstream API cannot be reached or response is truncated."""


def fetch_json(path: str, params: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
    qs = urllib.parse.urlencode(params)
    url = f"{BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except http.client.IncompleteRead as e:
        raise FetchError(f"IncompleteRead: {e}") from e
    except http.client.HTTPException as e:
        raise FetchError(f"HTTPException: {e}") from e
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        raise FetchError(str(e)) from e


def fetch_basketball_match_list(language: str = "en") -> list[dict[str, Any]]:
    """Fetch today's basketball match_list."""
    payload = fetch_json(
        "/magicball/v1/list/match_list",
        {
            "language": language,
            "cmp_type": "basketball",
            "tab_type": "all",
            "_t": int(datetime.now().timestamp() * 1000),
        },
    )
    data = payload.get("data") or {}
    return list(data.get("matches") or [])


def fetch_basketball_match_list_raw(language: str = "zh-cn") -> list[dict[str, Any]]:
    """Alias for raw basketball rows (no map_match)."""
    return fetch_basketball_match_list(language=language)


def schedule_start_param(beijing_day: str) -> str:
    """Nuxt converts Beijing calendar day → `start` for schedule_list.

    `Date.UTC(y, m-1, d) - 8h`, then format that instant's UTC fields as
    `YYYY-MM-DD HH:00:00` (e.g. 2026-07-22 → `2026-07-21 16:00:00`).
    """
    y, m, d = [int(x) for x in str(beijing_day).split("-")[:3]]
    utc_midnight = datetime(y, m, d, tzinfo=timezone.utc)
    shifted = utc_midnight - timedelta(hours=8)
    return shifted.strftime("%Y-%m-%d %H:00:00")


def fetch_basketball_schedule_list(
    beijing_day: str,
    *,
    language: str = "zh-CN",
    future: bool = True,
) -> list[dict[str, Any]]:
    """Fetch one Beijing calendar day via schedule_list (future or past tabs)."""
    payload = fetch_json(
        "/magicball/v1/list/schedule_list",
        {
            "language": language,
            "tab_type": "fixture" if future else "schedule",
            "cmp_type": "basketball",
            "start": schedule_start_param(beijing_day),
        },
    )
    data = payload.get("data") or {}
    return list(data.get("matches") or [])


def normalize_status(raw: str, minute: str, period: str = "") -> str:
    n = (raw or "").strip()
    key = n.lower()
    if key == "playing":
        return f"Playing {period or minute}".strip()
    if key == "played":
        return "Played"
    if key == "fixture":
        return "Fixture"
    if (period or "").upper() == "FT":
        return "Played"
    return n or "Unknown"


def to_local_start(start_play: str) -> tuple[str, str]:
    """API start_play string as UTC → (HH:MM, YYYY-MM-DD) Asia/Shanghai."""
    if not start_play:
        return "", ""
    try:
        dt = datetime.strptime(start_play, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        local = dt.astimezone(TZ_CN)
        return local.strftime("%H:%M"), local.strftime("%Y-%m-%d")
    except ValueError:
        return "", ""


def kickoff_from_raw(raw: dict[str, Any]) -> tuple[str, str, str]:
    """
    Return (HH:MM, YYYY-MM-DD, start_play_utc_str).

    Prefer match_timestamp — the API's start_play calendar date is often stale/wrong
    while match_timestamp is the real kickoff epoch.
    """
    ts = raw.get("match_timestamp") or raw.get("matchTimestamp")
    try:
        ts_i = int(ts) if ts not in (None, "", 0, "0") else 0
    except (TypeError, ValueError):
        ts_i = 0
    if ts_i > 0:
        dt = datetime.fromtimestamp(ts_i, tz=timezone.utc)
        local = dt.astimezone(TZ_CN)
        return (
            local.strftime("%H:%M"),
            local.strftime("%Y-%m-%d"),
            dt.strftime("%Y-%m-%d %H:%M:%S"),
        )
    time_str, local_date = to_local_start(raw.get("start_play") or "")
    return time_str, local_date, str(raw.get("start_play") or "")


def _score(side: dict[str, Any], is_fixture: bool) -> int | None:
    if is_fixture:
        return None
    fs = side.get("fs")
    if fs is None or fs == "":
        return 0
    try:
        return int(fs)
    except (TypeError, ValueError):
        return 0


def map_match(raw: dict[str, Any]) -> dict[str, Any]:
    team_a = raw.get("team_A") or {}
    team_b = raw.get("team_B") or {}
    comp = raw.get("competition") or {}
    status = raw.get("status") or ""
    is_fixture = status.lower() == "fixture"
    time_str, local_date, start_play = kickoff_from_raw(raw)
    business = int(raw.get("business_status") or 0)

    minute = str(raw.get("minute") or "")
    minute_str = str(raw.get("minute_str") or "") or minute
    period = str(raw.get("period") or raw.get("match_period") or "").upper()
    match_ts = int(raw.get("match_timestamp") or 0) or None
    update_ts = int(raw.get("update_timestamp") or 0) or None

    m = {
        "id": str(raw.get("match_id") or ""),
        "cmp_type": "basketball",
        "league_id": str(comp.get("id") or ""),
        "league": comp.get("name") or "Unknown",
        "league_color": comp.get("color") or "",
        "home": team_a.get("name") or "",
        "away": team_b.get("name") or "",
        "home_team_id": str(team_a.get("id") or ""),
        "away_team_id": str(team_b.get("id") or ""),
        "home_logo": team_a.get("logo") or "",
        "away_logo": team_b.get("logo") or "",
        "home_score": _score(team_a, is_fixture),
        "away_score": _score(team_b, is_fixture),
        "home_fs_4": team_a.get("fs_4") if team_a.get("fs_4") not in (None, "") else "",
        "away_fs_4": team_b.get("fs_4") if team_b.get("fs_4") not in (None, "") else "",
        "status_raw": status,
        "status": normalize_status(status, minute, period),
        "minute": minute,
        "minute_str": minute_str,
        "period": period,
        "start_play": start_play,
        "match_timestamp": match_ts,
        "update_timestamp": update_ts,
        "time": time_str,
        "local_date": local_date,
        "business_status": business,
        "flags": {"hot": (business & FLAG_HOT) == FLAG_HOT},
    }
    if m["home_score"] is not None and m["away_score"] is not None:
        m["score_diff"] = m["home_score"] - m["away_score"]
        m["score_total"] = m["home_score"] + m["away_score"]
    else:
        m["score_diff"] = None
        m["score_total"] = None
    m.update(progress_fields(m))
    m["tabs"] = match_tabs(m)
    return m


def format_wall_clock(elapsed_sec: int) -> str:
    if elapsed_sec < 0:
        elapsed_sec = 0
    hh, rem = divmod(int(elapsed_sec), 3600)
    mm, ss = divmod(rem, 60)
    if hh:
        return f"{hh:02d}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"


def progress_fields(m: dict[str, Any], *, now_ts: int | None = None) -> dict[str, Any]:
    """Basketball progress fields: period, game clock, and wall elapsed time."""
    status_raw = (m.get("status_raw") or "").lower()
    status_disp = str(m.get("status") or "").lower()
    if not status_raw:
        if status_disp.startswith("playing") or "进行中" in status_disp:
            status_raw = "playing"
        elif status_disp.startswith("played") or status_disp in ("ft", "完场"):
            status_raw = "played"
        elif status_disp.startswith("fixture") or status_disp in ("未开赛",):
            status_raw = "fixture"
    period = str(m.get("period") or "")

    if status_raw == "playing":
        official = " ".join(x for x in (period, str(m.get("minute_str") or "")) if x) or "Playing"
    elif status_raw == "played" or period.upper() == "FT":
        official = "FT"
    elif status_raw == "fixture":
        official = "未开赛"
    else:
        official = str(m.get("status") or status_raw or "")

    wall = ""
    wall_sec = None
    ts = m.get("match_timestamp")
    if ts and status_raw == "playing":
        now = int(now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp())
        wall_sec = max(0, now - int(ts))
        wall = format_wall_clock(wall_sec)
    elif status_raw == "fixture":
        wall = "--:--"
    elif status_raw == "played":
        wall = "结束"

    return {
        "official_clock": official,
        "wall_clock": wall,
        "wall_elapsed_sec": wall_sec,
        "period": period,
        "minute_str": str(m.get("minute_str") or ""),
    }


def is_hot(m: dict[str, Any]) -> bool:
    """Basketball hot-tab heuristic from DQD's business-status bitmask."""
    return (m.get("business_status", 0) & FLAG_HOT) == FLAG_HOT


def match_tabs(m: dict[str, Any]) -> list[str]:
    tabs = ["full"]
    if is_hot(m):
        tabs.append("hot")
    return tabs


TAB_FILTERS.update(
    {
        "full": lambda _m: True,
        "hot": is_hot,
    }
)


def today_cn() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d")


def day_window(start: str | None = None, days: int = 3) -> list[str]:
    """Beijing calendar dates: start .. start+days-1 (default today + next 2 days)."""
    days = max(1, int(days))
    start_s = start or today_cn()
    base = datetime.strptime(start_s, "%Y-%m-%d").replace(tzinfo=TZ_CN)
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def filter_today(matches: Iterable[dict[str, Any]], day: str | None = None) -> list[dict[str, Any]]:
    """Backward-compatible single-day filter."""
    return filter_days(matches, day=day, days=1)


def filter_days(
    matches: Iterable[dict[str, Any]],
    *,
    day: str | None = None,
    days: int = 3,
) -> list[dict[str, Any]]:
    allowed = set(day_window(day, days))
    items = list(matches)
    windowed = [m for m in items if m.get("local_date") in allowed]
    return windowed or items


def filter_tab(matches: Iterable[dict[str, Any]], tab: str) -> list[dict[str, Any]]:
    fn = TAB_FILTERS.get(tab)
    if fn is None:
        raise ValueError(f"unknown tab: {tab}")
    return [m for m in matches if fn(m)]


def league_summary(matches: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = defaultdict(int)
    names: dict[str, str] = {}
    for m in matches:
        lid = str(m.get("league_id") or "")
        name = m.get("league") or "Unknown"
        names[lid] = name
        counts[(lid, name)] += 1
    rows = [
        {"id": lid, "name": name, "count": counts[(lid, name)]}
        for (lid, name) in counts
    ]
    rows.sort(key=lambda r: (-r["count"], r["name"]))
    return rows


def has_live(matches: Iterable[dict[str, Any]]) -> bool:
    for m in matches:
        raw = (m.get("status_raw") or "").lower()
        status = m.get("status") or ""
        if raw == "playing" or status.startswith("Playing") or "进行中" in status:
            return True
    return False


def _map_basketball_list(language: str) -> list[dict[str, Any]]:
    raw = fetch_basketball_match_list(language=language)
    return [map_match(r) for r in raw if (r.get("cmp_type") or "basketball") == "basketball"]


def team_en_cache_path() -> Path:
    # scripts → dongqiudi-basketball → skills → .cursor → repo
    return Path(__file__).resolve().parents[4] / "data" / "dqd_team_en.json"


def load_team_en_cache() -> dict[str, str]:
    path = team_en_cache_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v}


def save_team_en_cache(cache: dict[str, str]) -> None:
    path = team_en_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fetch_team_en_name(team_id: str) -> str | None:
    """Resolve English team name via magicball team detail (`team_en_name`)."""
    tid = str(team_id or "").strip()
    if not tid:
        return None
    try:
        payload = fetch_json(
            "/magicball/v1/team/detail",
            {"team_id": tid, "language": "en"},
            timeout=12.0,
        )
    except (FetchError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    base = ((payload.get("data") or {}).get("base_info") or {}) if isinstance(payload, dict) else {}
    name = str(base.get("team_en_name") or "").strip()
    return name or None


def resolve_team_en_names(
    team_ids: Iterable[str],
    *,
    workers: int = 8,
    max_fetch: int = 64,
    fetch_timeout_s: float = 8.0,
) -> dict[str, str]:
    """Return team_id → English name, fetching + caching misses.

    Cold cache (after wiping ``data/``) can mean thousands of misses. Cap
    per-call fetches so match_list / bridge rematch stay responsive; remaining
    ids keep Chinese names until later ticks warm the cache.
    """
    cache = load_team_en_cache()
    missing = sorted(
        {
            str(t).strip()
            for t in team_ids
            if str(t).strip() and not cache.get(str(t).strip())
        }
    )
    if missing:
        batch = missing[: max(0, int(max_fetch))]
        if batch:
            deadline = time.monotonic() + max(0.5, float(fetch_timeout_s))
            with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
                futures = {pool.submit(fetch_team_en_name, tid): tid for tid in batch}
                try:
                    for fut in as_completed(
                        futures, timeout=max(0.1, deadline - time.monotonic())
                    ):
                        tid = futures[fut]
                        try:
                            name = fut.result(timeout=0)
                        except Exception:  # noqa: BLE001
                            name = None
                        if name:
                            cache[tid] = name
                        if time.monotonic() >= deadline:
                            break
                except FuturesTimeout:
                    pass
            save_team_en_cache(cache)
    return cache


def apply_english_team_names(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overwrite home/away with cached/fetched `team_en_name` values."""
    ids = [
        str(m.get("home_team_id") or "")
        for m in matches
        if m.get("home_team_id")
    ] + [
        str(m.get("away_team_id") or "")
        for m in matches
        if m.get("away_team_id")
    ]
    names = resolve_team_en_names(ids)
    for m in matches:
        hid = str(m.get("home_team_id") or "")
        aid = str(m.get("away_team_id") or "")
        if hid and names.get(hid):
            m["home"] = names[hid]
        if aid and names.get(aid):
            m["away"] = names[aid]
    return matches


def load_matches(
    language: str = "en",
    day: str | None = None,
    days: int = 3,
) -> list[dict[str, Any]]:
    """
    Load basketball matches for a Beijing date window (default: today + next 2 days).

    Sources (same as official /match page):
    - `match_list` — today tab (includes some early next-day kickoffs)
    - `schedule_list?tab_type=fixture` — each future day in the window
      (`start` must be the Nuxt-encoded datetime, not a bare date)

    For English (default): team names come from `team_en_name` via team detail
    (cached in `data/dqd_team_en.json`). Explicit `zh-cn` skips the rename.
    """
    day = day or today_cn()
    days = max(1, int(days))
    allowed = set(day_window(day, days))
    lang = str(language or "en").lower()

    by_id: dict[str, dict[str, Any]] = {}
    for m in _map_basketball_list("zh-cn"):
        mid = str(m.get("id") or "")
        if mid:
            by_id[mid] = m

    # Future calendar days are missing from match_list — pull schedule_list.
    for d in day_window(day, days):
        if d <= day:
            continue
        try:
            raw_rows = fetch_basketball_schedule_list(d, language="zh-CN", future=True)
        except (
            FetchError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
            http.client.IncompleteRead,
            http.client.HTTPException,
        ):
            continue
        for raw in raw_rows:
            if (raw.get("cmp_type") or "basketball") != "basketball":
                continue
            m = map_match(raw)
            mid = str(m.get("id") or "")
            if mid:
                by_id[mid] = m

    schedule = [m for m in by_id.values() if m.get("local_date") in allowed]
    if not schedule:
        schedule = list(by_id.values())
    schedule.sort(
        key=lambda m: (
            str(m.get("local_date") or ""),
            str(m.get("time") or ""),
            str(m.get("id") or ""),
        )
    )

    if lang in ("zh-cn", "zh", "cn"):
        return schedule

    return apply_english_team_names(schedule)


def build_snapshot(
    tab: str = "full",
    language: str = "en",
    day: str | None = None,
    days: int = 3,
    matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    day = day or today_cn()
    days = max(1, int(days))
    if matches is None:
        matches = load_matches(language=language, day=day, days=days)
    selected = filter_tab(matches, tab)
    dates = day_window(day, days)
    return {
        "fetched_at": datetime.now(TZ_CN).isoformat(timespec="seconds"),
        "language": language,
        "today": day,
        "days": days,
        "dates": dates,
        "tab": tab,
        "count": len(selected),
        "has_live": has_live(selected),
        "leagues": league_summary(selected),
        "matches": selected,
        "counts": {
            "full": len(matches),
            "hot": len(filter_tab(matches, "hot")),
        },
    }


def detect_score_changes(
    matches: Iterable[dict[str, Any]],
    prev_scores: dict[str, dict[str, Any]],
    tab: str,
) -> list[dict[str, Any]]:
    """Compare basketball scores and periods; emit every score delta and FT."""
    events: list[dict[str, Any]] = []
    ts = datetime.now(TZ_CN).isoformat(timespec="seconds")
    for m in matches:
        mid = str(m.get("id") or "")
        if not mid:
            continue
        curr_h, curr_a = m.get("home_score"), m.get("away_score")
        curr_period = str(m.get("period") or "").upper()
        if curr_h is None or curr_a is None:
            # Fixture / no score yet — still refresh baseline when first seen
            if mid not in prev_scores:
                prev_scores[mid] = {"home": curr_h, "away": curr_a, "period": curr_period}
            continue

        prev = prev_scores.get(mid)
        if prev is None:
            prev_scores[mid] = {"home": curr_h, "away": curr_a, "period": curr_period}
            continue

        prev_h, prev_a = prev.get("home"), prev.get("away")
        prev_period = str(prev.get("period") or "").upper()
        if prev_h is None or prev_a is None:
            if curr_period == "FT" and prev_period != "FT":
                events.append(
                    {
                        "type": "match_finished",
                        "ts": ts,
                        "match_id": mid,
                        "league": m.get("league") or "",
                        "league_id": m.get("league_id") or "",
                        "home": m.get("home") or "",
                        "away": m.get("away") or "",
                        "prev": {"home": prev_h, "away": prev_a},
                        "curr": {"home": curr_h, "away": curr_a},
                        "home_score": curr_h,
                        "away_score": curr_a,
                        "period": "FT",
                        "tab": tab,
                        "emit_downstream": True,
                    }
                )
            prev_scores[mid] = {"home": curr_h, "away": curr_a, "period": curr_period}
            continue

        dh = int(curr_h) - int(prev_h)
        da = int(curr_a) - int(prev_a)
        if dh != 0 or da != 0:
            side = "both" if dh > 0 and da > 0 else ("home" if dh > 0 else ("away" if da > 0 else "other"))
            is_score_up = dh > 0 or da > 0
            events.append(
                {
                    "type": "score_change",
                    "ts": ts,
                    "match_id": mid,
                    "league": m.get("league") or "",
                    "league_id": m.get("league_id") or "",
                    "home": m.get("home") or "",
                    "away": m.get("away") or "",
                    "prev": {"home": prev_h, "away": prev_a},
                    "curr": {"home": curr_h, "away": curr_a},
                    "home_score": curr_h,
                    "away_score": curr_a,
                    "side": side,
                    "is_score_up": is_score_up,
                    "is_goal": is_score_up,
                    "is_reversal": dh < 0 or da < 0,
                    "status": m.get("status") or "",
                    "tab": tab,
                    "minute": m.get("minute") or "",
                    "period": m.get("period") or "",
                    "fs_4": {"home": m.get("home_fs_4", ""), "away": m.get("away_fs_4", "")},
                    "score_diff": m.get("score_diff"),
                    "score_total": m.get("score_total"),
                    "minute_str": m.get("minute_str") or "",
                    "emit_downstream": True,
                }
            )
        if curr_period == "FT" and prev_period != "FT":
            events.append(
                {
                    "type": "match_finished",
                    "ts": ts,
                    "match_id": mid,
                    "league": m.get("league") or "",
                    "league_id": m.get("league_id") or "",
                    "home": m.get("home") or "",
                    "away": m.get("away") or "",
                    "prev": {"home": prev_h, "away": prev_a},
                    "curr": {"home": curr_h, "away": curr_a},
                    "home_score": curr_h,
                    "away_score": curr_a,
                    "period": "FT",
                    "tab": tab,
                    "emit_downstream": True,
                }
            )
        prev_scores[mid] = {"home": curr_h, "away": curr_a, "period": curr_period}
    return events


def events_for_downstream(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ev for ev in events if ev.get("emit_downstream") is not False]


def boxscore_path(match_id: str) -> str:
    return f"/soccer/biz/dqd/bkb_match/lineup/{urllib.parse.quote(str(match_id))}"


def fetch_boxscore(match_id: str) -> dict[str, Any]:
    """Fetch a playing match's basketball lineup, trying API then web origin."""
    params = {"app": "dqd", "lang": "zh-cn"}
    qs = urllib.parse.urlencode(params)
    errors: list[str] = []
    for origin in ("https://api.dongqiudi.com", "https://www.dongqiudi.com"):
        url = f"{origin}{boxscore_path(match_id)}?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=12.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, http.client.HTTPException) as exc:
            errors.append(f"{origin}: {exc}")
    raise FetchError("; ".join(errors))


def person_en_cache_path() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "dqd_person_en.json"


def _load_name_cache(path: Path) -> dict[str, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in raw.items() if v} if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_person_en_name(person_id: str) -> str | None:
    try:
        payload = fetch_json("/magicball/v1/person/detail", {"person_id": person_id, "language": "en"}, timeout=12)
    except FetchError:
        return None
    data = payload.get("data") or {}
    for source in (data, data.get("base_info") or {}):
        for key in ("person_en_name", "name_en", "en_name", "name"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return None


def apply_english_person_names(players: dict[str, dict[str, Any]], *, max_fetch: int = 32) -> None:
    """Add cached/fetched ``person_en_name`` values to normalized players."""
    path = person_en_cache_path()
    cache = _load_name_cache(path)
    misses = [pid for pid in players if not cache.get(pid)][:max_fetch]
    if misses:
        with ThreadPoolExecutor(max_workers=min(8, len(misses))) as pool:
            for pid, name in zip(misses, pool.map(fetch_person_en_name, misses)):
                if name:
                    cache[pid] = name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for pid, player in players.items():
        player["person_en_name"] = cache.get(pid, "")


_STAT_KEYS = {
    "score": ("score", "points", "point", "pts"),
    "rebound": ("rebound", "rebounds", "reb"),
    "assist": ("assist", "assists", "ast"),
    "steal": ("steal", "steals", "stl"),
    "block": ("block", "blocks", "blk"),
}


def _number(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def extract_player_stats(boxscore: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Best-effort normalize heterogeneous DQD basketball lineup response."""
    players: dict[str, dict[str, Any]] = {}
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            pid = value.get("person_id") or value.get("player_id") or value.get("id")
            name = value.get("person_name") or value.get("player_name") or value.get("name")
            stats_source = value.get("stats") if isinstance(value.get("stats"), dict) else value
            stats = {out: next((n for key in keys if (n := _number(stats_source.get(key))) is not None), 0) for out, keys in _STAT_KEYS.items()}
            if pid and name and any(stats.values()):
                players[str(pid)] = {"person_id": str(pid), "person_name": str(name), **stats}
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(boxscore.get("data") or boxscore)
    return players


def detect_player_stat_changes(
    match: dict[str, Any], current: dict[str, dict[str, Any]], previous: dict[str, dict[str, Any]], tab: str
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    ts = datetime.now(TZ_CN).isoformat(timespec="seconds")
    for pid, player in current.items():
        old = previous.get(pid, {})
        changes = {key: player[key] - int(old.get(key, 0) or 0) for key in _STAT_KEYS if player[key] > int(old.get(key, 0) or 0)}
        if old and changes:
            events.append({"type": "player_stat_change", "ts": ts, "match_id": match["id"], "person_id": pid,
                           "person_name": player["person_name"], "person_en_name": player.get("person_en_name") or "",
                           "changes": changes, "curr": {key: player[key] for key in _STAT_KEYS},
                           "period": match.get("period") or "", "tab": tab, "emit_downstream": True})
    return events


def safe_load_matches(
    language: str = "en",
    day: str | None = None,
    days: int = 3,
) -> list[dict[str, Any]]:
    try:
        return load_matches(language=language, day=day, days=days)
    except FetchError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, http.client.IncompleteRead, http.client.HTTPException) as e:
        raise FetchError(str(e)) from e

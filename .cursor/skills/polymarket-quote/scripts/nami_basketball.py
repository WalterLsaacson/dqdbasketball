"""Nami basketball MQTT + HTTP snapshot (protobuf, no external deps).

Endpoints:
  GET https://tracker-api.namitiyu.com/api/basketball/variable_detail?id={nami_id}&lang=zh
  MQTT wss://trackermq.namitiyu.com/mqtt  topic live/m1/{nami_id}

Push vc.code: 10101 MLive (score), 10102 Stats, 10103 Odds, 10105 StatsId.
"""

from __future__ import annotations

import logging
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("pm_quote.nami_basketball")

MQTT_HOST = "trackermq.namitiyu.com"
MQTT_PATH = "/mqtt"
MQTT_PORT = 443
MQTT_ORIGIN = "https://tracker.namitiyu.com"
API_ROOT = "https://tracker-api.namitiyu.com"
PUSH_MLIVE = 10101
PUSH_STATS = 10102
HTTP_TIMEOUT_S_DEFAULT = 2.0


def read_varint(buf: bytes, i: int) -> tuple[int, int]:
    n = 0
    shift = 0
    while i < len(buf):
        x = buf[i]
        i += 1
        n |= (x & 0x7F) << shift
        if x < 0x80:
            return n, i
        shift += 7
        if shift > 70:
            raise ValueError("varint overflow")
    raise ValueError("eof")


def decode_fields(buf: bytes) -> list[tuple[int, str, object]]:
    i = 0
    out: list[tuple[int, str, object]] = []
    while i < len(buf):
        try:
            key, i = read_varint(buf, i)
        except ValueError:
            break
        fn, wt = key >> 3, key & 7
        if wt == 0:
            try:
                v, i = read_varint(buf, i)
            except ValueError:
                break
            out.append((fn, "varint", v))
        elif wt == 1:
            if i + 8 > len(buf):
                break
            i += 8
        elif wt == 2:
            try:
                ln, i = read_varint(buf, i)
            except ValueError:
                break
            if ln < 0 or i + ln > len(buf):
                break
            data = buf[i : i + ln]
            i += ln
            out.append((fn, "bytes", data))
        elif wt == 5:
            if i + 4 > len(buf):
                break
            i += 4
        else:
            break
    return out


def unwrap_api(raw: bytes) -> tuple[int, bytes]:
    code = 0
    data = b""
    for fn, typ, v in decode_fields(raw):
        if fn == 1 and typ == "varint":
            code = int(v)
        if fn == 2 and typ == "bytes":
            data = v  # type: ignore[assignment]
    return code, data


def _score_map(buf: bytes) -> dict[str, int]:
    names = {1: "score", 2: "halfScore", 6: "overTime", 7: "penalty"}
    out: dict[str, int] = {}
    for fn, typ, v in decode_fields(buf):
        if fn in names:
            out[names[fn]] = int(v)
    return out


def _timer_map(buf: bytes) -> dict[str, int]:
    names = {1: "ticking", 2: "countdown", 3: "uptime", 4: "second", 5: "addTime"}
    out: dict[str, int] = {}
    for fn, typ, v in decode_fields(buf):
        if fn in names:
            out[names[fn]] = int(v)
    return out


def _stats_map(buf: bytes) -> list[dict[str, int]]:
    items: list[dict[str, int]] = []
    for fn, typ, v in decode_fields(buf):
        if fn == 1 and typ == "bytes":
            row: dict[str, int] = {}
            for f2, t2, v2 in decode_fields(v):  # type: ignore[arg-type]
                if f2 == 1:
                    row["code"] = int(v2)
                elif f2 == 2:
                    row["home"] = int(v2)
                elif f2 == 3:
                    row["away"] = int(v2)
            if row:
                items.append(row)
    return items


def decode_basketball_variable_detail(raw: bytes) -> dict[str, Any]:
    _code, data = unwrap_api(raw)
    obj: dict[str, Any] = {}
    for fn, typ, v in decode_fields(data):
        if fn == 1:
            obj["id"] = int(v)
        elif fn == 2:
            obj["statusId"] = int(v)
        elif fn == 3 and typ == "bytes":
            obj["homeScores"] = _score_map(v)  # type: ignore[arg-type]
        elif fn == 4 and typ == "bytes":
            obj["awayScores"] = _score_map(v)  # type: ignore[arg-type]
        elif fn == 5 and typ == "bytes":
            obj["timer"] = _timer_map(v)  # type: ignore[arg-type]
        elif fn == 7 and typ == "bytes":
            obj["stats"] = _stats_map(v)  # type: ignore[arg-type]
    return obj


def parse_ss(ss: str) -> tuple[int, int]:
    parts = str(ss or "").replace(":", "-").split("-")
    nums: list[int] = []
    for p in parts:
        p = p.strip()
        if p.isdigit() or (p.startswith("-") and p[1:].isdigit()):
            nums.append(int(p))
        else:
            nums.append(0)
    while len(nums) < 2:
        nums.append(0)
    return nums[0], nums[1]


def nami_id_from_url(url: Any) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        qs = urlparse(text)
        return str((parse_qs(qs.query).get("id") or [""])[0] or "").strip()
    except ValueError:
        return ""


def topic_for(nami_id: str) -> str:
    return f"live/m1/{str(nami_id).strip()}"


def fetch_variable_detail(nami_id: str, *, timeout_s: float | None = None) -> dict[str, Any]:
    timeout_s = float(
        timeout_s
        if timeout_s is not None
        else _env_float("NAMI_HTTP_TIMEOUT_S", HTTP_TIMEOUT_S_DEFAULT)
    )
    url = f"{API_ROOT}/api/basketball/variable_detail?id={nami_id}&lang=zh"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Origin": MQTT_ORIGIN,
            "Referer": f"{MQTT_ORIGIN}/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()
    return decode_basketball_variable_detail(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class NamiBasketballClient:
    """HTTP-poll snapshot for one match (MQTT upgrade path later)."""

    def __init__(self, nami_id: str, *, timeout_s: float | None = None) -> None:
        self.nami_id = str(nami_id or "").strip()
        self.timeout_s = timeout_s
        self._last: dict[str, Any] = {}

    def refresh(self) -> dict[str, Any]:
        data = fetch_variable_detail(self.nami_id, timeout_s=self.timeout_s)
        if isinstance(data, dict) and data:
            self._last = data
        return self._last

    @property
    def last(self) -> dict[str, Any]:
        return self._last

    def scores(self) -> tuple[int, int]:
        home = int((self._last.get("homeScores") or {}).get("score") or 0)
        away = int((self._last.get("awayScores") or {}).get("score") or 0)
        return home, away

    def status_id(self) -> int:
        return int(self._last.get("statusId") or 0)

    def timer(self) -> dict[str, int]:
        return dict(self._last.get("timer") or {})

    def stats(self) -> list[dict[str, int]]:
        return list(self._last.get("stats") or [])


class NamiScorePoller:
    """Optional score source; first observation is a baseline, never a trade."""

    def __init__(self, *, interval_s: float = 5.0) -> None:
        self.interval_s = max(1.0, float(interval_s))
        self._next_poll = 0.0
        self._previous: dict[str, tuple[int, int]] = {}
        self._id_map: dict[str, Any] = {}
        try:
            value = json.loads(os.getenv("NAMI_MATCH_IDS_JSON", "{}"))
            if isinstance(value, dict):
                self._id_map = value
        except json.JSONDecodeError:
            logger.warning("NAMI_MATCH_IDS_JSON is not valid JSON")

    def poll(self, matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now = time.monotonic()
        if now < self._next_poll:
            return []
        self._next_poll = now + self.interval_s
        events: list[dict[str, Any]] = []
        candidates: list[tuple[str, dict[str, Any], str, bool]] = []
        for row in matches[:20]:
            dqd = row.get("dongqiudi") or {}
            mid = str(dqd.get("id") or "")
            if not mid or row.get("finished"):
                continue
            mapping = self._id_map.get(mid) or dqd.get("nami_id")
            if isinstance(mapping, dict):
                nami_id = str(mapping.get("id") or "")
                swap = bool(mapping.get("swap"))
            else:
                nami_id = str(mapping or "")
                swap = False
            if not nami_id.isdigit():
                continue
            candidates.append((mid, dqd, nami_id, swap))
        if not candidates:
            return events
        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
            jobs = {
                pool.submit(fetch_variable_detail, nami_id): (mid, dqd, nami_id, swap)
                for mid, dqd, nami_id, swap in candidates
            }
            for future in as_completed(jobs):
                mid, dqd, nami_id, swap = jobs[future]
                try:
                    snap = future.result()
                    home = (snap.get("homeScores") or {}).get("score")
                    away = (snap.get("awayScores") or {}).get("score")
                    if home is None or away is None:
                        continue
                    current = (int(away), int(home)) if swap else (int(home), int(away))
                    if any(v < 0 or v > 500 for v in current):
                        continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Nami poll failed match=%s id=%s: %s", mid, nami_id, exc)
                    continue
                previous = self._previous.get(mid)
                self._previous[mid] = current
                if previous is None or current == previous:
                    continue
                events.append({
                    "type": "score_change", "source": "nami_http", "match_id": mid,
                    "home": dqd.get("home") or "", "away": dqd.get("away") or "",
                    "prev": {"home": previous[0], "away": previous[1]},
                    "curr": {"home": current[0], "away": current[1]},
                    "home_score": current[0], "away_score": current[1],
                    "is_reversal": current[0] < previous[0] or current[1] < previous[1],
                    "nami_id": nami_id,
                })
        return events

#!/usr/bin/env python3
"""Polymarket Board server: bridges polymarket-basketball skill and serves the frontend."""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

MODULE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = MODULE_DIR.parent
ROOT = FRONTEND_DIR.parent
PUBLIC = MODULE_DIR / "public"
SRC = MODULE_DIR / "src"
SCRIPTS = ROOT / ".cursor" / "skills" / "polymarket-basketball" / "scripts"
DATA = ROOT / "data" / "polymarket"

sys.path.insert(0, str(SCRIPTS))
import pm_lib as lib  # noqa: E402
from pm_basket import data_dir, write_json  # noqa: E402

HOST = "127.0.0.1"
PORT = 8888
MODULE_ID = "polymarket-board"


def parse_league_arg(raw: str | None) -> list[str] | None:
    if not raw or raw.strip().lower() in ("all", "*", ""):
        return None
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


class FetchState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.running = False
        self.league = "all"
        self.include_closed = False
        self.within_hours = int(getattr(lib, "DEFAULT_WITHIN_HOURS", 48))
        self.max_per_league = 100
        self.interval = 600  # 10 minutes — fixture list changes slowly
        self.thread: threading.Thread | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.started_at: str | None = None
        self.ticks = 0
        self._stop = threading.Event()

    def _status_unlocked(self) -> dict[str, Any]:
        return {
            "module": MODULE_ID,
            "running": self.running,
            "league": self.league,
            "include_closed": self.include_closed,
            "within_hours": self.within_hours,
            "interval": self.interval,
            "started_at": self.started_at,
            "ticks": self.ticks,
            "last_error": self.last_error,
            "proxy": (self.last_result or {}).get("proxy"),
            "window": (self.last_result or {}).get("window"),
            "last_result": {
                "fetched_at": (self.last_result or {}).get("fetched_at"),
                "count": (self.last_result or {}).get("count"),
                "proxy": (self.last_result or {}).get("proxy"),
                "window": (self.last_result or {}).get("window"),
                "leagues": (self.last_result or {}).get("leagues") or [],
                "matches": (self.last_result or {}).get("matches") or [],
            }
            if self.last_result
            else None,
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self._status_unlocked()

    def fetch_once(
        self,
        league: str = "all",
        *,
        include_closed: bool = False,
        max_per_league: int = 100,
        within_hours: int = 48,
    ) -> dict[str, Any]:
        leagues = parse_league_arg(league)
        payload = lib.load_matches(
            leagues,
            include_closed=include_closed,
            max_per_league=max_per_league,
            within_hours=within_hours,
        )
        write_json(data_dir(str(DATA)) / "snapshot.json", payload)
        with self.lock:
            self.league = league or "all"
            self.include_closed = include_closed
            self.within_hours = int(within_hours)
            self.max_per_league = max_per_league
            self.last_result = payload
            self.last_error = None
            self.ticks += 1
        return payload

    def start(
        self,
        league: str,
        *,
        include_closed: bool = False,
        within_hours: int = 48,
        interval: int = 600,
        max_per_league: int = 100,
    ) -> dict[str, Any]:
        with self.lock:
            if self.running:
                return {"ok": True, "already": True, **self._status_unlocked()}
            self.league = league or "all"
            self.include_closed = include_closed
            self.within_hours = int(within_hours)
            self.max_per_league = max_per_league
            self.interval = max(120, interval)  # floor 2 min; default 10 min
            self._stop.clear()
            self.running = True
            self.last_error = None
            self.started_at = datetime.now(lib.TZ_CN).isoformat(timespec="seconds")
            self.thread = threading.Thread(target=self._loop, name="pm-fetch", daemon=True)
            self.thread.start()
            return {"ok": True, "already": False, **self._status_unlocked()}

    def stop(self) -> dict[str, Any]:
        with self.lock:
            self._stop.set()
            self.running = False
            thread = self.thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        with self.lock:
            self.thread = None
            return {"ok": True, **self._status_unlocked()}

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = 600
            try:
                with self.lock:
                    league = self.league
                    include_closed = self.include_closed
                    within_hours = self.within_hours
                    max_per_league = self.max_per_league
                    interval = self.interval
                # First tick already fetched in /api/fetch/start; wait then refresh.
                if self._stop.wait(interval):
                    break
                self.fetch_once(
                    league,
                    include_closed=include_closed,
                    max_per_league=max_per_league,
                    within_hours=within_hours,
                )
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.last_error = str(e)
                traceback.print_exc()
                self._stop.wait(60)
        with self.lock:
            self.running = False


FETCH = FetchState()


def _load_stale_snapshot() -> dict[str, Any] | None:
    path = data_dir(str(DATA)) / "snapshot.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def json_response(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _safe_file(root: Path, rel: str) -> Path | None:
    path = (root / rel).resolve()
    if not str(path).startswith(str(root.resolve())) or not path.is_file():
        return None
    return path


def serve_file(handler: BaseHTTPRequestHandler, path: Path) -> None:
    ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if path.suffix == ".js":
        ctype = "text/javascript; charset=utf-8"
    elif path.suffix == ".css":
        ctype = "text/css; charset=utf-8"
    elif path.suffix == ".html":
        ctype = "text/html; charset=utf-8"
    data = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.wfile.write(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "PolymarketBoard/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            file_path = PUBLIC / "index.html"
            if file_path.is_file():
                serve_file(self, file_path)
            else:
                self.send_error(404)
            return
        if path.startswith("/src/"):
            file_path = _safe_file(SRC, path[len("/src/") :])
            if file_path:
                serve_file(self, file_path)
            else:
                self.send_error(404)
            return
        if path == "/api/health":
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "module": MODULE_ID,
                    "skill": "polymarket-basketball",
                    "version": "1.0.0",
                },
            )
            return
        if path == "/api/module":
            meta = MODULE_DIR / "module.json"
            if meta.is_file():
                json_response(self, 200, json.loads(meta.read_text(encoding="utf-8")))
            else:
                json_response(self, 404, {"error": "module.json missing"})
            return
        if path == "/api/status":
            json_response(self, 200, FETCH.status())
            return
        if path == "/api/leagues":
            try:
                catalog = lib.basketball_league_catalog()
                payload = {
                    "fetched_at": datetime.now(lib.TZ_CN).isoformat(timespec="seconds"),
                    "source": "polymarket-gamma",
                    "proxy": lib.resolve_proxy(None) or "direct",
                    "count": len(catalog),
                    "leagues": catalog,
                }
                write_json(data_dir(str(DATA)) / "leagues.json", payload)
                json_response(self, 200, payload)
            except lib.FetchError as e:
                json_response(self, 502, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                json_response(self, 500, {"error": str(e)})
            return
        if path == "/api/matches":
            league = (qs.get("league") or ["all"])[0]
            include_closed = (qs.get("include_closed") or ["0"])[0] in ("1", "true", "yes")
            max_n = int((qs.get("max") or ["100"])[0])
            within_hours = int(
                (qs.get("within_hours") or [str(getattr(lib, "DEFAULT_WITHIN_HOURS", 48))])[0]
            )
            # Default: serve skill buffer (snapshot). refresh=1 forces Gamma pull.
            want_refresh = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
            if not want_refresh:
                cached = None
                with FETCH.lock:
                    if FETCH.last_result:
                        cached = dict(FETCH.last_result)
                if not cached:
                    cached = _load_stale_snapshot()
                if cached:
                    out = dict(cached)
                    out["from_cache"] = True
                    json_response(self, 200, out)
                    return
                # No buffer yet — fall through to one live fetch.
            try:
                payload = FETCH.fetch_once(
                    league,
                    include_closed=include_closed,
                    max_per_league=max_n,
                    within_hours=within_hours,
                )
                payload = dict(payload)
                payload["from_cache"] = False
                json_response(self, 200, payload)
            except lib.FetchError as e:
                stale = _load_stale_snapshot()
                if stale:
                    stale = dict(stale)
                    stale["stale"] = True
                    stale["from_cache"] = True
                    stale["error"] = str(e)
                    json_response(self, 200, stale)
                else:
                    json_response(self, 502, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                json_response(self, 500, {"error": str(e)})
            return

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        body = read_body(self)

        if path == "/api/fetch/once":
            league = str(body.get("league") or "all")
            include_closed = bool(body.get("include_closed"))
            max_n = int(body.get("max") or 100)
            within_hours = int(
                body.get("within_hours")
                if body.get("within_hours") is not None
                else getattr(lib, "DEFAULT_WITHIN_HOURS", 48)
            )
            try:
                payload = FETCH.fetch_once(
                    league,
                    include_closed=include_closed,
                    max_per_league=max_n,
                    within_hours=within_hours,
                )
                json_response(self, 200, payload)
            except lib.FetchError as e:
                json_response(self, 502, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                json_response(self, 500, {"error": str(e)})
            return
        if path == "/api/fetch/start":
            league = str(body.get("league") or "all")
            include_closed = bool(body.get("include_closed"))
            interval = int(body.get("interval") or 600)
            max_n = int(body.get("max") or 100)
            within_hours = int(
                body.get("within_hours")
                if body.get("within_hours") is not None
                else getattr(lib, "DEFAULT_WITHIN_HOURS", 48)
            )
            # Kick an immediate fetch so UI has data before the loop sleeps.
            try:
                FETCH.fetch_once(
                    league,
                    include_closed=include_closed,
                    max_per_league=max_n,
                    within_hours=within_hours,
                )
            except Exception as e:  # noqa: BLE001
                json_response(self, 502, {"error": str(e)})
                return
            json_response(
                self,
                200,
                FETCH.start(
                    league,
                    include_closed=include_closed,
                    within_hours=within_hours,
                    interval=interval,
                    max_per_league=max_n,
                ),
            )
            return
        if path == "/api/fetch/stop":
            json_response(self, 200, FETCH.stop())
            return

        self.send_error(404)


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Polymarket Board → http://{HOST}:{PORT}/", flush=True)
    print(f"Module path       → {MODULE_DIR}", flush=True)
    print(f"Skill scripts     → {SCRIPTS}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
        FETCH.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

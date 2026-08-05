#!/usr/bin/env python3
"""Match Board server: bridges dongqiudi-basketball skill and serves the frontend module."""

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
SCRIPTS = ROOT / ".cursor" / "skills" / "dongqiudi-basketball" / "scripts"
DATA = ROOT / "data"

sys.path.insert(0, str(SCRIPTS))
import dqd_lib as lib  # noqa: E402
from dqd_match import data_dir, emit_sentinels, run_watch_once, write_json  # noqa: E402

HOST = "127.0.0.1"
PORT = 8887
MODULE_ID = "match-board"


class WatchState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.running = False
        self.tab = "full"
        self.interval = 15
        self.idle_interval = 60
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
            "tab": self.tab,
            "interval": self.interval,
            "idle_interval": self.idle_interval,
            "started_at": self.started_at,
            "ticks": self.ticks,
            "last_error": self.last_error,
            "last_result": {
                "fetched_at": (self.last_result or {}).get("fetched_at"),
                "count": (self.last_result or {}).get("count"),
                "changes": (self.last_result or {}).get("changes"),
                "has_live": (self.last_result or {}).get("has_live"),
                "events": (self.last_result or {}).get("events") or [],
                "matches": (self.last_result or {}).get("matches"),
                "leagues": lib.league_summary((self.last_result or {}).get("matches") or []),
            }
            if self.last_result
            else None,
        }

    def status(self) -> dict[str, Any]:
        with self.lock:
            return self._status_unlocked()

    def start(self, tab: str, interval: int, idle_interval: int) -> dict[str, Any]:
        with self.lock:
            if self.running:
                return {"ok": True, "already": True, **self._status_unlocked()}
            self.tab = tab
            # Official-like cadence: 10–15s live, 30–60s idle.
            self.interval = max(10, interval)
            self.idle_interval = max(max(30, self.interval), idle_interval)
            self._stop.clear()
            self.running = True
            self.last_error = None
            self.started_at = datetime.now(lib.TZ_CN).isoformat(timespec="seconds")
            self.ticks = 0
            self.thread = threading.Thread(target=self._loop, name="dqd-watch", daemon=True)
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
        ddir = data_dir(str(DATA))
        while not self._stop.is_set():
            try:
                with self.lock:
                    tab = self.tab
                    interval = self.interval
                    idle_interval = self.idle_interval
                result = run_watch_once(tab, "en", ddir, quiet=False)
                emit_sentinels(result.get("events") or [])
                with self.lock:
                    self.last_result = result
                    self.last_error = None
                    self.ticks += 1
                    has_live = bool(result.get("has_live"))
                sleep_s = interval if has_live else idle_interval
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    self.last_error = str(e)
                sleep_s = 10
                traceback.print_exc()
            self._stop.wait(sleep_s)
        with self.lock:
            self.running = False


WATCH = WatchState()


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
    server_version = "DQDMatchBoard/1.0"

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
        # Backward-compatible aliases from the early web/ demo
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            if rel == "app.js":
                rel = "main.js"
            if rel == "styles.css" or rel.endswith(".js") or rel.endswith(".css"):
                file_path = _safe_file(SRC, rel)
            else:
                file_path = _safe_file(PUBLIC, rel)
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
                    "skill": "dongqiudi-basketball",
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
            json_response(self, 200, WATCH.status())
            return
        if path == "/api/matches":
            tab = (qs.get("tab") or ["full"])[0]
            if tab not in ("full", "hot"):
                json_response(self, 400, {"error": "invalid tab"})
                return
            try:
                snap = lib.build_snapshot(tab, language="en")
                write_json(data_dir(str(DATA)) / "snapshot.json", snap)
                json_response(self, 200, snap)
            except lib.FetchError as e:
                json_response(self, 502, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                json_response(self, 500, {"error": str(e)})
            return
        if path == "/api/watch/once":
            tab = (qs.get("tab") or ["full"])[0]
            if tab not in ("full", "hot"):
                json_response(self, 400, {"error": "invalid tab"})
                return
            try:
                result = run_watch_once(tab, "en", data_dir(str(DATA)), quiet=False)
                json_response(self, 200, result)
            except lib.FetchError as e:
                json_response(self, 502, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                json_response(self, 500, {"error": str(e)})
            return
        if path == "/api/events":
            limit = int((qs.get("limit") or ["50"])[0])
            path_e = data_dir(str(DATA)) / "events.jsonl"
            rows: list[dict[str, Any]] = []
            if path_e.exists():
                lines = path_e.read_text(encoding="utf-8").splitlines()
                for line in lines[-max(1, limit) :]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            json_response(self, 200, {"events": rows})
            return

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        body = read_body(self)

        if path == "/api/watch/start":
            tab = str(body.get("tab") or "full")
            if tab not in ("full", "hot"):
                json_response(self, 400, {"error": "invalid tab"})
                return
            interval = int(body.get("interval") or 15)
            idle = int(body.get("idle_interval") or 60)
            json_response(self, 200, WATCH.start(tab, interval, idle))
            return
        if path == "/api/watch/stop":
            json_response(self, 200, WATCH.stop())
            return

        self.send_error(404)


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    PUBLIC.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Match Board module → http://{HOST}:{PORT}/", flush=True)
    print(f"Module path        → {MODULE_DIR}", flush=True)
    print(f"Skill scripts      → {SCRIPTS}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
        WATCH.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

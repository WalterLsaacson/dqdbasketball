#!/usr/bin/env python3
"""System Main — boot quote skill cascade + all frontend boards."""

from __future__ import annotations

import json
import mimetypes
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

MODULE_DIR = Path(__file__).resolve().parents[1]
FRONTEND = MODULE_DIR.parent
ROOT = FRONTEND.parent
PUBLIC = MODULE_DIR / "public"
SRC = MODULE_DIR / "src"

HOST = "127.0.0.1"
PORT = 8890

BOARDS = (
    {
        "id": "match-board",
        "name": "Dongqiudi",
        "script": FRONTEND / "run.py",
        "port": 8887,
        "skill": "dongqiudi-basketball",
    },
    {
        "id": "polymarket-board",
        "name": "Polymarket",
        "script": FRONTEND / "run_polymarket.py",
        "port": 8888,
        "skill": "polymarket-basketball",
    },
    {
        "id": "bridge-board",
        "name": "Match Bridge",
        "script": FRONTEND / "run_bridge.py",
        "port": 8889,
        "skill": "match-bridge",
    },
)

QUOTE_SCRIPT = (
    ROOT / ".cursor" / "skills" / "polymarket-quote" / "scripts" / "pm_quote.py"
)

_children: list[subprocess.Popen[Any]] = []
_lock = threading.RLock()
_started_at: str | None = None
_quote_proc: subprocess.Popen[Any] | None = None
_quote_trade: dict[str, Any]
_shutting_down = threading.Event()
_httpd: ThreadingHTTPServer | None = None
_supervisor_stop = threading.Event()
_supervisor_thread: threading.Thread | None = None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_mode(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    m = raw.strip().lower()
    if m in ("dry", "live"):
        return m
    if m in ("1", "true", "yes", "on"):
        return "live"
    if m in ("0", "false", "no", "off"):
        return "dry"
    return None


def load_quote_trade_config(
    *,
    live: bool | None = None,
    score_mode: str | None = None,
    prop_mode: str | None = None,
    ft_mode: str | None = None,
    no_trade: bool | None = None,
    take_depth: str | None = None,
    max_levels: int | None = None,
    max_usdc: float | None = None,
    max_shares: float | None = None,
    max_slippage: float | None = None,
    allow_extreme_prices: bool | None = None,
    min_buy_price: float | None = None,
    interval: float | None = None,
    trade_env_file: str | None = None,
) -> dict[str, Any]:
    """Merge CLI overrides with QUOTE_* env (defaults = dry-run trade on)."""
    depth = (take_depth or os.getenv("QUOTE_TAKE_DEPTH") or "top").strip().lower()
    if depth not in ("top", "walk"):
        depth = "top"

    enabled = _env_bool("QUOTE_TRADE", True)
    if no_trade is True:
        enabled = False
    elif no_trade is False:
        enabled = True

    env_file = trade_env_file or os.getenv("QUOTE_TRADE_ENV_FILE") or None
    if not env_file and (ROOT / ".env").is_file():
        env_file = str(ROOT / ".env")

    # --live / QUOTE_LIVE sets all channels live; per-channel modes override.
    base_live = bool(live) if live is not None else _env_bool("QUOTE_LIVE", False)
    s_mode = score_mode if score_mode is not None else (
        _env_mode("QUOTE_SCORE_MODE") or _env_mode("QUOTE_GOALS_MODE")
    )
    p_mode = prop_mode if prop_mode is not None else _env_mode("QUOTE_PROP_MODE")
    f_mode = ft_mode if ft_mode is not None else _env_mode("QUOTE_FT_MODE")
    if s_mode is None:
        s_mode = "live" if base_live else "dry"
    if p_mode is None:
        p_mode = "live" if base_live else "dry"
    if f_mode is None:
        f_mode = "live" if base_live else "dry"

    return {
        "enabled": enabled,
        "live": s_mode == "live" or p_mode == "live" or f_mode == "live",
        "score_mode": s_mode,
        "prop_mode": p_mode,
        "ft_mode": f_mode,
        "take_depth": depth,
        "max_levels": int(
            max_levels if max_levels is not None else os.getenv("QUOTE_MAX_LEVELS", "5")
        ),
        "max_usdc": float(
            max_usdc if max_usdc is not None else os.getenv("QUOTE_MAX_USDC", "20")
        ),
        "max_shares": float(
            max_shares if max_shares is not None else os.getenv("QUOTE_MAX_SHARES", "25")
        ),
        "max_slippage": float(
            max_slippage
            if max_slippage is not None
            else os.getenv("QUOTE_MAX_SLIPPAGE", "0.03")
        ),
        "allow_extreme_prices": (
            bool(allow_extreme_prices)
            if allow_extreme_prices is not None
            else _env_bool("QUOTE_ALLOW_EXTREME_PRICES", False)
        ),
        "min_buy_price": float(
            min_buy_price
            if min_buy_price is not None
            else os.getenv("QUOTE_MIN_BUY_PRICE", "0.92")
        ),
        "interval": max(
            0.05,
            float(
                interval if interval is not None else os.getenv("QUOTE_INTERVAL", "0.25")
            ),
        ),
        "trade_env_file": env_file,
    }


def quote_watch_argv(cfg: dict[str, Any] | None = None) -> list[str]:
    """Build pm_quote.py watch argv including in-process trade flags."""
    c = cfg or _quote_trade
    args: list[str] = ["watch", "--interval", str(float(c["interval"]))]
    if not c.get("enabled", True):
        args.append("--no-trade")
        return args
    s = str(c.get("score_mode") or "dry")
    p = str(c.get("prop_mode") or "dry")
    f = str(c.get("ft_mode") or "dry")
    if s == "live" and p == "live" and f == "live":
        args.append("--live")
    else:
        args.extend(["--score-mode", s])
        args.extend(["--prop-mode", p])
        args.extend(["--ft-mode", f])
    args.extend(["--take-depth", str(c.get("take_depth") or "top")])
    args.extend(["--max-levels", str(int(c.get("max_levels") or 5))])
    args.extend(["--max-usdc", str(float(c.get("max_usdc") or 20))])
    args.extend(["--max-shares", str(float(c.get("max_shares") or 25))])
    args.extend(["--max-slippage", str(float(c.get("max_slippage") or 0.03))])
    _mbp = c.get("min_buy_price")
    args.extend(
        ["--min-buy-price", str(float(0.0 if _mbp is None else _mbp))]
    )
    if c.get("allow_extreme_prices"):
        args.append("--allow-extreme-prices")
    env_file = c.get("trade_env_file")
    if env_file:
        args.extend(["--trade-env-file", str(env_file)])
    return args


# Default trade config (env + .env path); main() may override via CLI.
_quote_trade = load_quote_trade_config()


def _now() -> str:
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _port_open(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _http_json(url: str, *, timeout: float = 1.5) -> dict[str, Any] | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "main-module/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return None


def _http_post_json(
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 8.0,
) -> dict[str, Any] | None:
    try:
        body = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "User-Agent": "main-module/1.0",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return None


BRIDGE_BOARD_PORT = 8889


def _spawn(script: Path, *args: str, log_path: Path | None = None) -> subprocess.Popen[Any]:
    stdout: Any = subprocess.DEVNULL
    stderr: Any = subprocess.DEVNULL
    log_fh = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
        stdout = log_fh
        stderr = subprocess.STDOUT
    proc = subprocess.Popen(
        [sys.executable, str(script), *args],
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    # Keep file handle alive on the process object so it is not GC'd closed.
    if log_fh is not None:
        proc._quote_log_fh = log_fh  # type: ignore[attr-defined]
    return proc


def _boards_all_up() -> bool:
    return all(_port_open(int(b["port"])) for b in BOARDS)


def _wait_port(port: int, *, timeout_s: float, poll_s: float = 0.15) -> bool:
    """Wait for a port without holding ``_lock``."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _shutting_down.is_set() or _supervisor_stop.is_set():
            return False
        if _port_open(port):
            return True
        time.sleep(poll_s)
    return _port_open(port)


def _ensure_boards() -> list[dict[str, Any]]:
    """Start any board whose port is down. Returns launch records.

    Caller must hold ``_lock`` (or be single-threaded boot).
    """
    launched: list[dict[str, Any]] = []
    for board in BOARDS:
        port = int(board["port"])
        url = f"http://{HOST}:{port}/"
        if _port_open(port):
            launched.append(
                {"id": board["id"], "port": port, "url": url, "already": True}
            )
            continue
        print(f"main → starting {board['id']} @ {url}", flush=True)
        proc = _spawn(Path(board["script"]))
        _children.append(proc)
        launched.append(
            {
                "id": board["id"],
                "port": port,
                "url": url,
                "already": False,
                "pid": proc.pid,
            }
        )
    return launched


def _trade_mode_label(trade: dict[str, Any]) -> str:
    if not trade.get("enabled"):
        return "off"
    return (
        f"score:{trade.get('score_mode', 'dry')} "
        f"prop:{trade.get('prop_mode', 'dry')} "
        f"ft:{trade.get('ft_mode', 'dry')}"
    )


def _ensure_quote() -> bool:
    """Start quote watch if missing. Returns True if (re)started.

    Caller must hold ``_lock``.
    """
    global _quote_proc
    if _quote_proc is not None and _quote_proc.poll() is None:
        return False
    watch_args = quote_watch_argv(_quote_trade)
    trade = _quote_trade
    mode = _trade_mode_label(trade)
    print(
        "main → starting polymarket-quote watch "
        f"(trade={mode} depth={trade.get('take_depth')} "
        f"max_usdc={trade.get('max_usdc')}) "
        "(→ in-process basketball match-bridge → DQD + PM)",
        flush=True,
    )
    quote_log = ROOT / "data" / "pm-quote" / "watch.log"
    _quote_proc = _spawn(QUOTE_SCRIPT, *watch_args, log_path=quote_log)
    _children.append(_quote_proc)
    return True


def ensure_stack(*, open_browser: bool = False) -> dict[str, Any]:
    """One-shot: all basketball boards + quote cascade. Heals missing children."""
    global _started_at
    if _shutting_down.is_set():
        return {"ok": False, "error": "hub_shutting_down"}

    already_up = False
    with _lock:
        quote_alive = bool(_quote_proc and _quote_proc.poll() is None)
        already_up = bool(_started_at and quote_alive and _boards_all_up())
        launched: list[dict[str, Any]] = []
        if not already_up:
            launched = _ensure_boards()

    # HTTP / port waits outside _lock so /api/status stays responsive.
    if already_up:
        return status()

    _wait_port(BRIDGE_BOARD_PORT, timeout_s=20)

    with _lock:
        if _shutting_down.is_set():
            return {"ok": False, "error": "hub_shutting_down"}
        _ensure_quote()
        if _started_at is None:
            _started_at = _now()
        _start_supervisor()

        if open_browser:
            threading.Thread(
                target=_open_uis, args=(launched,), name="open-uis", daemon=True
            ).start()

        trade = _quote_trade
        return {
            "ok": True,
            "started_at": _started_at,
            "boards": launched,
            "quote_pid": _quote_proc.pid if _quote_proc else None,
            "quote_trade": dict(trade),
            "quote_argv": quote_watch_argv(trade),
            "hub": f"http://{HOST}:{PORT}/",
        }


def start_stack(*, open_browser: bool = True) -> dict[str, Any]:
    """Start all basketball boards and the quote cascade."""
    return ensure_stack(open_browser=open_browser)


def _supervisor_loop() -> None:
    """Keep boards + quote up while the stack is marked started."""
    while not _supervisor_stop.wait(5.0):
        if _shutting_down.is_set():
            return
        healed_boards = False
        with _lock:
            if _started_at is None or _shutting_down.is_set():
                continue
            try:
                healed_boards = not _boards_all_up()
                if healed_boards:
                    print("main → supervisor: board(s) down, restarting…", flush=True)
                    _ensure_boards()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                continue
        # Port wait must not hold _lock (status / start would stall).
        if healed_boards:
            _wait_port(BRIDGE_BOARD_PORT, timeout_s=15)
        with _lock:
            if _started_at is None or _shutting_down.is_set():
                continue
            try:
                if _ensure_quote():
                    print("main → supervisor: quote watch restarted", flush=True)
            except Exception:  # noqa: BLE001
                traceback.print_exc()


def _start_supervisor() -> None:
    global _supervisor_thread
    if _supervisor_thread is not None and _supervisor_thread.is_alive():
        return
    _supervisor_stop.clear()
    _supervisor_thread = threading.Thread(
        target=_supervisor_loop, name="main-supervisor", daemon=True
    )
    _supervisor_thread.start()


def _open_uis(launched: list[dict[str, Any]]) -> None:
    time.sleep(0.8)
    urls = [f"http://{HOST}:{PORT}/"] + [b["url"] for b in launched]
    for url in urls:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif sys.platform.startswith("linux"):
                subprocess.Popen(
                    ["xdg-open", url],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.25)


def stop_stack() -> dict[str, Any]:
    """Stop boards + quote children; disable supervisor heal."""
    global _quote_proc, _started_at
    _supervisor_stop.set()
    with _lock:
        procs = list(_children)
        _children.clear()
        _quote_proc = None
        _started_at = None
    for p in procs:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                try:
                    p.send_signal(signal.SIGTERM)
                except Exception:  # noqa: BLE001
                    pass
    for p in procs:
        try:
            p.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True, "stopped": True}


def request_hub_shutdown() -> None:
    """Stop children and ask the HTTP server to exit (safe from signal / request threads)."""
    if _shutting_down.is_set():
        return
    _shutting_down.set()
    print("\nshutting down stack…", flush=True)
    stop_stack()
    server = _httpd
    if server is not None:
        threading.Thread(
            target=server.shutdown, name="httpd-shutdown", daemon=True
        ).start()


def status() -> dict[str, Any]:
    boards_st: list[dict[str, Any]] = []
    for board in BOARDS:
        port = int(board["port"])
        up = _port_open(port)
        extra: dict[str, Any] = {}
        if up and board["id"] == "bridge-board":
            st = _http_json(f"http://{HOST}:{port}/api/status")
            if st:
                extra = {
                    "skill_running": st.get("running"),
                    "dqd_ticks": st.get("dqd_ticks"),
                    "pm_ticks": st.get("pm_ticks"),
                }
        boards_st.append(
            {
                "id": board["id"],
                "name": board["name"],
                "skill": board["skill"],
                "port": port,
                "url": f"http://{HOST}:{port}/",
                "up": up,
                **extra,
            }
        )
    with _lock:
        q = _quote_proc
        quote_alive = bool(q and q.poll() is None)
        started = _started_at
        trade = dict(_quote_trade)
    mode = _trade_mode_label(trade)
    return {
        "module": "main",
        "hub": f"http://{HOST}:{PORT}/",
        "started_at": started,
        "quote": {
            "skill": "polymarket-quote",
            "running": quote_alive,
            "pid": q.pid if quote_alive and q else None,
            "cascade": [
                "match-bridge",
                "dongqiudi-basketball",
                "polymarket-basketball",
            ],
            "trade": {
                **trade,
                "mode": mode,
                "trades_path": str(ROOT / "data" / "pm-quote" / "trades.jsonl"),
                "watch_log": str(ROOT / "data" / "pm-quote" / "watch.log"),
            },
        },
        "boards": boards_st,
        "ok": True,
    }


def json_response(handler: BaseHTTPRequestHandler, code: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    try:
        return json.loads(handler.rfile.read(n).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/health":
            json_response(self, 200, {"ok": True, "module": "main", "port": PORT})
            return
        if path == "/api/status":
            json_response(self, 200, status())
            return

        if path == "/" or path == "/index.html":
            return self._file(PUBLIC / "index.html", "text/html; charset=utf-8")
        if path.startswith("/src/"):
            rel = path[len("/src/") :]
            fp = SRC / rel
            if fp.is_file() and SRC in fp.resolve().parents:
                ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
                return self._file(fp, ctype)
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        body = read_body(self)
        if path == "/api/start":
            try:
                json_response(
                    self,
                    200,
                    start_stack(open_browser=bool(body.get("open_browser", False))),
                )
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                json_response(self, 502, {"error": str(e)})
            return
        if path == "/api/stop":
            # Respond first, then exit hub (do not leave empty shell on :8890).
            json_response(
                self,
                200,
                {"ok": True, "stopped": True, "hub_exiting": True},
            )
            request_hub_shutdown()
            return
        self.send_error(404)

    def _file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main(argv: list[str] | None = None) -> int:
    import argparse

    global _quote_trade, _httpd

    parser = argparse.ArgumentParser(
        description=(
            "System Main — hub + basketball boards + quote "
            "(do not start pm_quote / boards separately)"
        )
    )
    parser.add_argument("--no-trade", action="store_true", help="Quote only (no executor)")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Post real CLOB orders for score, player-stat, and FT signals",
    )
    parser.add_argument(
        "--score-mode",
        choices=("dry", "live"),
        default=None,
        help="score_change dry|live (default dry; --live sets live unless overridden)",
    )
    parser.add_argument(
        "--prop-mode",
        choices=("dry", "live"),
        default=None,
        help="player_stat_change dry|live (default dry; --live sets live unless overridden)",
    )
    parser.add_argument(
        "--ft-mode",
        choices=("dry", "live"),
        default=None,
        help="match_finished dry|live (default dry; --live sets live unless overridden)",
    )
    parser.add_argument(
        "--take-depth",
        choices=("top", "walk"),
        default=None,
        help="Fill depth (default top / QUOTE_TAKE_DEPTH)",
    )
    parser.add_argument("--max-levels", type=int, default=None)
    parser.add_argument("--max-usdc", type=float, default=None)
    parser.add_argument("--max-shares", type=float, default=None)
    parser.add_argument("--max-slippage", type=float, default=None)
    parser.add_argument("--allow-extreme-prices", action="store_true")
    parser.add_argument(
        "--min-buy-price",
        type=float,
        default=None,
        help="buy_win floor (default 0.92; 0=off; env QUOTE_MIN_BUY_PRICE)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Quote max idle seconds / events.jsonl wake (default 0.25)",
    )
    parser.add_argument(
        "--trade-env-file",
        default=None,
        help="Env file for PRIVATE_KEY/FUNDER (default repo .env)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open browser tabs on boot",
    )
    args = parser.parse_args(argv)

    _quote_trade = load_quote_trade_config(
        live=True if args.live else None,
        score_mode=args.score_mode,
        prop_mode=args.prop_mode,
        ft_mode=args.ft_mode,
        no_trade=True if args.no_trade else None,
        take_depth=args.take_depth,
        max_levels=args.max_levels,
        max_usdc=args.max_usdc,
        max_shares=args.max_shares,
        max_slippage=args.max_slippage,
        allow_extreme_prices=True if args.allow_extreme_prices else None,
        min_buy_price=args.min_buy_price,
        interval=args.interval,
        trade_env_file=args.trade_env_file,
    )

    PUBLIC.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)

    if _port_open(PORT):
        print(
            f"error: System Main already running on http://{HOST}:{PORT}/ — "
            "do not start a second hub; stop the existing one first "
            f"(POST /api/stop or kill the run_main process).",
            file=sys.stderr,
            flush=True,
        )
        return 1

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    _httpd = httpd
    print(f"System Main → http://{HOST}:{PORT}/", flush=True)
    t = _quote_trade
    if not t["enabled"]:
        trade_label = "off"
    else:
        trade_label = (
            f"score:{t.get('score_mode', 'dry')} "
            f"prop:{t.get('prop_mode', 'dry')} ft:{t.get('ft_mode', 'dry')}"
        )
    print(
        f"Quote trade → {trade_label} "
        f"depth={t['take_depth']} max_usdc={t['max_usdc']} "
        f"min_buy_price={t.get('min_buy_price', 0.0)}",
        flush=True,
    )
    print("Booting skills + boards…", flush=True)

    open_browser = not args.no_browser

    def _boot() -> None:
        try:
            start_stack(open_browser=open_browser)
            st = status()
            qt = (st.get("quote") or {}).get("trade") or {}
            print(
                f"Stack ready · quote={'up' if st['quote']['running'] else 'down'} "
                f"· trade={qt.get('mode')} · "
                + " · ".join(
                    f"{b['id']}={'up' if b['up'] else 'down'}" for b in st["boards"]
                ),
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            print(f"Boot failed: {e}", flush=True)

    threading.Thread(target=_boot, name="main-boot", daemon=True).start()

    def _on_signal(_sig: int, _frame: object) -> None:
        request_hub_shutdown()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        request_hub_shutdown()
    finally:
        if not _shutting_down.is_set():
            stop_stack()
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass
        _httpd = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

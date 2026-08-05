#!/usr/bin/env python3
"""Bridge Board server: runs match-bridge skill and serves the frontend."""

from __future__ import annotations

import json
import mimetypes
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

MODULE_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = MODULE_DIR.parent
ROOT = FRONTEND_DIR.parent
PUBLIC = MODULE_DIR / "public"
SRC = MODULE_DIR / "src"
BRIDGE_SCRIPTS = ROOT / ".cursor" / "skills" / "match-bridge" / "scripts"
DQD_SCRIPTS = ROOT / ".cursor" / "skills" / "dongqiudi-basketball" / "scripts"

sys.path.insert(0, str(BRIDGE_SCRIPTS))
sys.path.insert(0, str(DQD_SCRIPTS))
import bridge_lib as bridge  # noqa: E402

HOST = "127.0.0.1"
PORT = 8889
MODULE_ID = "bridge-board"

RUNTIME = bridge.BridgeRuntime(ROOT)


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


def refresh_clocks(payload: dict[str, Any]) -> dict[str, Any]:
    """Recompute wall/official clocks on each API read for live rows."""
    try:
        import dqd_lib as dqd  # type: ignore
    except Exception:  # noqa: BLE001
        return payload
    matches = []
    for row in payload.get("matches") or []:
        item = dict(row)
        dqd_part = dict(item.get("dongqiudi") or {})
        dqd_part.update(dqd.progress_fields(dqd_part))
        item["dongqiudi"] = dqd_part
        matches.append(item)
    out = dict(payload)
    out["matches"] = matches
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "BridgeBoard/1.0"

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
                    "skill": "match-bridge",
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
            st = RUNTIME.status()
            owner = ROOT / "data" / "bridge" / ".inproc_owner"
            owned = owner.is_file()
            st["inproc_owner"] = owned
            st["viewer_mode"] = (
                "quote_owned"
                if owned
                else ("board_skill" if st.get("running") else "idle")
            )
            if owned:
                try:
                    st["inproc_owner_text"] = owner.read_text(encoding="utf-8").strip()
                except OSError:
                    st["inproc_owner_text"] = ""
            json_response(self, 200, st)
            return
        if path == "/api/matches":
            snap = bridge.load_json(ROOT / "data" / "bridge" / "matches.json", None)
            if not snap:
                # Prefer in-memory payload when this process owns the skill;
                # otherwise empty until quote/System Main writes files.
                if RUNTIME.last_result:
                    snap = RUNTIME.last_result
                else:
                    json_response(
                        self,
                        200,
                        {
                            "matched_at": None,
                            "count": 0,
                            "matches": [],
                            "events": [],
                            "note": "read-only until match-bridge writes data/bridge",
                        },
                    )
                    return
            out = refresh_clocks(snap)
            # Surface in-memory events from the latest rematch tick (file may be stale mid-tick).
            if RUNTIME.last_result and RUNTIME.last_result.get("events"):
                out = dict(out)
                out["events"] = list(RUNTIME.last_result.get("events") or [])
            json_response(self, 200, out)
            return
        if path == "/api/events":
            qs = parse_qs(parsed.query)
            limit = int((qs.get("limit") or ["50"])[0])
            path_e = ROOT / "data" / "bridge" / "events.jsonl"
            rows: list[dict[str, Any]] = []
            if path_e.is_file():
                for line in path_e.read_text(encoding="utf-8").splitlines()[-max(1, limit) :]:
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

        if path == "/api/bridge/once":
            owner = ROOT / "data" / "bridge" / ".inproc_owner"
            if owner.is_file():
                # Read-only: quote owns the skill; just re-serve file snapshot.
                snap = bridge.load_json(ROOT / "data" / "bridge" / "matches.json", {}) or {}
                out = refresh_clocks(snap) if snap else {
                    "matched_at": None,
                    "count": 0,
                    "matches": [],
                    "note": "quote owns match-bridge; showing file snapshot",
                }
                out["ok"] = True
                out["viewer_mode"] = "quote_owned"
                json_response(self, 200, out)
                return
            offline = bool(body.get("offline"))
            try:
                payload = RUNTIME.run_once(refresh=not offline)
                json_response(self, 200, refresh_clocks(payload))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                json_response(self, 502, {"error": str(e)})
            return
        if path == "/api/bridge/start":
            owner = ROOT / "data" / "bridge" / ".inproc_owner"
            if owner.is_file():
                snap = bridge.load_json(ROOT / "data" / "bridge" / "matches.json", {}) or {}
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "already": True,
                        "running": False,
                        "viewer_mode": "quote_owned",
                        "note": (
                            "match-bridge is owned by polymarket-quote (in-process). "
                            "Board is read-only; Start is a no-op."
                        ),
                        "last_result": refresh_clocks(snap) if snap else None,
                    },
                )
                return
            if body.get("tab"):
                RUNTIME.dqd_tab = str(body.get("tab"))
            try:
                result = RUNTIME.start()
                # Include latest matches for UI.
                snap = RUNTIME.last_result or bridge.load_json(
                    ROOT / "data" / "bridge" / "matches.json", {}
                )
                result["last_result"] = refresh_clocks(snap) if snap else result.get("last_result")
                result["viewer_mode"] = "board_skill"
                json_response(self, 200, result)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                json_response(self, 502, {"error": str(e)})
            return
        if path == "/api/bridge/stop":
            json_response(self, 200, RUNTIME.stop())
            return

        self.send_error(404)


def main() -> int:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "bridge").mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Bridge Board → http://{HOST}:{PORT}/", flush=True)
    print(f"Module path  → {MODULE_DIR}", flush=True)
    print(
        "Skill        → match-bridge (read-only UI; Start watch / Sync once, "
        "or let polymarket-quote own in-process bridge)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)
        RUNTIME.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

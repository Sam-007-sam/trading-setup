"""
Pre-Market Desk local server (used by the Mac app).

Serves the dashboard on http://127.0.0.1:<port>/ and exposes:
  GET  /api/status   refresh state, used by the page to show progress
  POST /api/refresh  fetch fresh data now (ignored if one ran in the last 60 s)
  POST /api/ping     heartbeat from the open page
  POST /api/quit     stop the server
A refresh starts automatically when the server starts. The server stops by itself
a few minutes after the dashboard window is closed (no more heartbeats).
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import http.server
import importlib
import json
import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

IDLE_EXIT_SECONDS = 240
STATE = {"app": "premarket", "status": "idle", "started": None, "finished": None,
         "error": None, "problems": []}
LOCK = threading.Lock()
LAST_PING = time.time()
LAST_DONE = 0.0


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def run_refresh() -> None:
    global LAST_DONE
    with LOCK:
        if STATE["status"] == "running" or time.time() - LAST_DONE < 60:
            return
        STATE.update(status="running", started=now_iso(), error=None, problems=[])
    try:
        import fetch
        importlib.reload(fetch)          # picks up a fresh PROBLEMS list each run
        fetch.main()
        try:   # backtest once a day; it needs several years of data
            bt = os.path.join(os.environ.get("PMD_DATA", ""), "backtest.json")
            if not os.path.exists(bt) or time.time() - os.path.getmtime(bt) > 20 * 3600:
                import backtest
                import intraday
                backtest.main()
                intraday.main()
        except Exception:
            traceback.print_exc()
        try:   # paper ledger is cheap; refresh every time
            import paper
            paper.main()
        except Exception:
            traceback.print_exc()
        STATE.update(status="done", finished=now_iso(), problems=list(fetch.PROBLEMS))
    except Exception as e:
        traceback.print_exc()
        STATE.update(status="error", finished=now_iso(), error=str(e))
    LAST_DONE = time.time()


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the log quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if self.path.startswith("/data/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        global LAST_PING
        if self.path.startswith("/api/status"):
            LAST_PING = time.time()
            return self._json(STATE)
        return super().do_GET()

    def do_POST(self):
        global LAST_PING
        LAST_PING = time.time()
        if self.path.startswith("/api/refresh"):
            threading.Thread(target=run_refresh, daemon=True).start()
            return self._json({"ok": True})
        if self.path.startswith("/api/ping"):
            return self._json({"ok": True})
        if self.path.startswith("/api/watchlist"):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                items = json.loads(self.rfile.read(min(n, 100_000)))["items"]
                clean = [{"sym": str(i["sym"])[:20].upper(), "note": str(i.get("note", ""))[:120]}
                         for i in items][:50]
                path = os.path.join(os.environ.get("PMD_DATA", ""), "watchlist.json")
                with open(path, "w") as f:
                    json.dump({"items": clean}, f, indent=1)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if self.path.startswith("/api/quit"):
            self._json({"ok": True})
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return
        self._json({"error": "not found"}, 404)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--site", required=True)
    ap.add_argument("--no-refresh", action="store_true")
    args = ap.parse_args()

    handler = functools.partial(Handler, directory=args.site)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)

    def watchdog():
        while True:
            time.sleep(15)
            if time.time() - LAST_PING > IDLE_EXIT_SECONDS and STATE["status"] != "running":
                print("No open window for a while; stopping.", flush=True)
                os._exit(0)

    threading.Thread(target=watchdog, daemon=True).start()
    if not args.no_refresh:
        threading.Thread(target=run_refresh, daemon=True).start()
    print(f"Serving on http://127.0.0.1:{args.port}/", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

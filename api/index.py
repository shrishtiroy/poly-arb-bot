"""Vercel serverless entrypoint for the polyarb dashboard.

Vercel runs stateless Python functions, not the long-lived http.server the
`polyarb dashboard` CLI uses. This adapts the same render functions into a
serverless handler: Vercel looks for a class named `handler` subclassing
BaseHTTPRequestHandler and calls it per request.

IMPORTANT: the collector (a persistent websocket streamer) cannot run here.
This function only SERVES a pre-populated SQLite snapshot. The database path
comes from the POLYARB_DB env var (default: a `data/live.sqlite` committed
alongside the deploy). If the file is absent, pages render their empty state.
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

# Make the src/ layout importable when Vercel runs this file from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from polyarb.dashboard import (  # noqa: E402
    render,
    render_about,
    render_fairvalue,
    render_ladders,
)

# Read-only snapshot bundled with the deploy (or pointed at by env var). The
# live collector writes this on a real host; Vercel only serves it.
DB = os.environ.get("POLYARB_DB",
                    str(Path(__file__).resolve().parent.parent / "data" / "live.sqlite"))

ROUTES = {
    "/": render,
    "/index.html": render,
    "/ladders": render_ladders,
    "/fair-value": render_fairvalue,
    "/about": lambda _db: render_about(),
}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Strip any query string; match only the path.
        path = self.path.split("?", 1)[0]
        renderer = ROUTES.get(path)
        if renderer is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return
        try:
            html = renderer(DB).encode()
        except Exception as exc:  # never 500 silently: surface the reason
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"dashboard error: {exc}".encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def log_message(self, *_a):
        pass

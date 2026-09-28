#!/usr/bin/env python3
"""Read-only fake Bindery API for known-queue staging acceptance."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlparse


STATE = Path(os.environ.get("FAKE_BINDERY_STATE", "/state/state.json"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(fmt % args, flush=True)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        state = json.loads(STATE.read_text(encoding="utf-8"))
        status = 200
        if path == "/api/v1/system/status":
            payload = {"status": "ok"}
        elif path.startswith("/api/v1/setting/"):
            key = unquote(path.split("/setting/", 1)[1])
            settings = {
                "import.mode": "external",
                "import.drop_folder": "/data/bookguard-staging",
                "import.drop_layout": "flat",
                "import.drop_link_mode": "copy",
                "autoGrab.enabled": "false",
            }
            status = 200 if key in settings else 404
            payload = {"key": key, "value": settings[key]} if status == 200 else {"error": "not found"}
        elif path == "/api/v1/book/101":
            payload = {
                "id": 101, "title": "Progress Fixture",
                "authorName": "Fixture Author",
                "ebookFilePath": "", "bookFiles": [],
            }
        elif path == "/api/v1/queue":
            status = int(state.get("queueStatus", 200))
            payload = (
                {"items": state["queue"], "partial": bool(state.get("partial", False))}
                if status == 200 else {"error": "injected queue failure"}
            )
        else:
            status, payload = 404, {"error": "not found", "path": path}
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        self.send_error(405, "No Bindery mutation is allowed in this scenario.")

    def do_DELETE(self) -> None:
        self.send_error(405, "No Bindery mutation is allowed in this scenario.")

    def do_PUT(self) -> None:
        self.send_error(405, "No Bindery mutation is allowed in this scenario.")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

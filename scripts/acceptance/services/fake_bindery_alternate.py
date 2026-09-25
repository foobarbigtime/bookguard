#!/usr/bin/env python3
"""Disposable Bindery API: count exact grabs, never scan or admit media."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Lock
from urllib.parse import unquote, urlparse


STATE = Path(os.environ.get("FAKE_BINDERY_STATE", "/state/state.json"))
LOCK = Lock()
ALTERNATE = {
    "guid": "alternate-guid",
    "title": "Fixture Author - Alternate Fixture alternate epub",
    "mediaType": "ebook", "protocol": "usenet", "approved": True,
    "nzbUrl": "https://indexer.invalid/disposable.nzb",
    "size": 2048, "indexerName": "Disposable Indexer",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(fmt % args, flush=True)

    def reply(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        state = json.loads(STATE.read_text(encoding="utf-8"))
        if path == "/api/v1/system/status":
            self.reply(200, {"status": "ok"})
        elif path.startswith("/api/v1/setting/"):
            key = unquote(path.split("/setting/", 1)[1])
            settings = {
                "import.mode": "external",
                "import.drop_folder": "/data/bookguard-staging",
                "import.drop_layout": "flat",
                "import.drop_link_mode": "copy",
                "autoGrab.enabled": "false",
            }
            self.reply(
                200 if key in settings else 404,
                {"value": settings[key]} if key in settings else {"error": "not found"},
            )
        elif path == "/api/v1/book/101":
            self.reply(200, {
                "id": 101, "title": "Alternate Fixture", "authorName": "Fixture Author",
                "ebookFilePath": "", "bookFiles": [],
            })
        elif path == "/api/v1/queue":
            self.reply(200, {"items": state["queue"], "partial": False})
        elif path == "/api/v1/history":
            self.reply(200, {"items": [], "partial": False})
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/api/v1/book/101/search":
            self.reply(200, {"results": [ALTERNATE]})
            return
        if path != "/api/v1/queue/grab":
            self.reply(405, {"error": "Only the disposable grab is supported"})
            return
        if body.get("guid") != ALTERNATE["guid"] or body.get("bookId") != 101:
            self.reply(409, {"error": "Unexpected release or book"})
            return
        with LOCK:
            state = json.loads(STATE.read_text(encoding="utf-8"))
            state["grabAttempts"] += 1
            if state["grabAttempts"] != 1 or state["queue"]:
                STATE.write_text(json.dumps(state), encoding="utf-8")
                self.reply(409, {"error": "Duplicate grab refused"})
                return
            state["queue"] = [{
                "id": 77, "bookId": 101, "title": ALTERNATE["title"],
                "protocol": "usenet", "status": "downloading",
            }]
            STATE.write_text(json.dumps(state), encoding="utf-8")
        self.reply(200, {"queueItem": state["queue"][0]})

    def do_DELETE(self) -> None:
        self.reply(405, {"error": "No delete allowed"})

    def do_PUT(self) -> None:
        self.reply(405, {"error": "No settings mutation allowed"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

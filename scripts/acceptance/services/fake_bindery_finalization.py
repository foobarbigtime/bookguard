#!/usr/bin/env python3
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse


STATE = Path(os.environ.get("FAKE_BINDERY_STATE", "/state/state.json"))
BOOK_ID = int(os.environ.get("FAKE_BINDERY_BOOK_ID", "101"))


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_state(value: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE)


class Handler(BaseHTTPRequestHandler):
    server_version = "BookGuardAcceptanceFakeBindery/1"

    def log_message(self, fmt: str, *args) -> None:
        print(fmt % args, flush=True)

    def send_json(self, status: int, payload: object | None = None) -> None:
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        state = load_state()
        if parsed.path == "/health":
            self.send_json(200, {"ok": True})
            return
        if parsed.path == f"/api/v1/book/{BOOK_ID}":
            status = int(state.get("getBookStatus", 200))
            if status >= 400:
                self.send_json(status, {"error": "injected get-book failure"})
                return
            files = []
            if state.get("registered", True):
                files.append({
                    "id": 501,
                    "format": "ebook",
                    "path": state["storedPath"],
                })
            self.send_json(200, {
                "id": BOOK_ID,
                "title": "Finalization Fixture",
                "authorName": "Finalization Author",
                "ebookFilePath": state["storedPath"] if files else "",
                "bookFiles": files,
            })
            return
        if parsed.path == "/api/v1/queue":
            status = int(state.get("queueStatus", 200))
            if status >= 400:
                self.send_json(status, {"error": "injected queue failure"})
                return
            self.send_json(200, {
                "items": list(state.get("queue", [])),
                "partial": bool(state.get("partial", False)),
            })
            return
        if parsed.path == "/api/v1/system/status":
            self.send_json(200, {"status": "ok", "acceptance": True})
            return
        self.send_json(404, {"error": "not found", "path": parsed.path})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/v1/queue/"):
            self.send_json(404, {"error": "not found"})
            return
        try:
            queue_id = int(parsed.path.rsplit("/", 1)[-1])
        except ValueError:
            self.send_json(400, {"error": "invalid queue id"})
            return

        state = load_state()
        state["deleteAttempts"] = int(state.get("deleteAttempts", 0)) + 1
        params = parse_qs(parsed.query)
        remove_from_client = (params.get("removeFromClient") or [""])[0]
        delete_files = (params.get("deleteFiles") or [""])[0]
        state["lastRemoveFromClient"] = remove_from_client
        state["lastDeleteFiles"] = delete_files
        save_state(state)

        injected = int(state.get("deleteStatus", 204))
        if injected >= 400:
            self.send_json(injected, {"error": "injected delete failure"})
            return
        if remove_from_client != "false" or delete_files != "false":
            self.send_json(409, {"error": "unsafe cleanup flags"})
            return

        queue = [
            item
            for item in state.get("queue", [])
            if str(item.get("id") or "") != str(queue_id)
        ]
        state["queue"] = queue
        state["deleteSuccesses"] = int(state.get("deleteSuccesses", 0)) + 1
        save_state(state)
        self.send_json(204)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

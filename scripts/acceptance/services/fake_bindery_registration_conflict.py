#!/usr/bin/env python3
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, unquote, urlparse


STATE = Path(os.environ.get("FAKE_BINDERY_STATE", "/state/state.json"))
DB = Path(os.environ.get("FAKE_BINDERY_DB", "/state/bindery.db"))
BOOK_ID = int(os.environ.get("FAKE_BINDERY_BOOK_ID", "101"))
STORED_PATH = os.environ.get(
    "FAKE_BINDERY_STORED_PATH",
    "/data/media/books/BookGuard Test/Conflict Fixture.epub",
)


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8"))


def save_state(value: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE)


def current_owner() -> int | None:
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute(
            "SELECT book_id FROM book_files WHERE path=? AND format='ebook' LIMIT 1",
            (STORED_PATH,),
        ).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def set_owner(book_id: int) -> None:
    conn = sqlite3.connect(DB)
    try:
        cursor = conn.execute(
            "UPDATE book_files SET book_id=? WHERE path=? AND format='ebook'",
            (int(book_id), STORED_PATH),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("expected exactly one ebook association to update")
        conn.commit()
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "BookGuardAcceptanceConflictBindery/1"

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

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        state = load_state()
        if parsed.path == "/health":
            self.send_json(200, {"ok": True})
            return
        if parsed.path == "/api/v1/system/status":
            self.send_json(200, {"status": "ok", "acceptance": True})
            return
        if parsed.path.startswith("/api/v1/setting/"):
            key = unquote(parsed.path.split("/setting/", 1)[1])
            value = (state.get("settings") or {}).get(key)
            if value is None:
                self.send_json(404, {"error": "setting not found", "key": key})
                return
            self.send_json(200, {"key": key, "value": value})
            return
        if parsed.path == f"/api/v1/book/{BOOK_ID}":
            owner = current_owner()
            files = []
            if owner == BOOK_ID:
                files.append({"id": 9001, "format": "ebook", "path": STORED_PATH})
            self.send_json(
                200,
                {
                    "id": BOOK_ID,
                    "title": "Conflict Fixture",
                    "authorName": "Conflict Author",
                    "ebookFilePath": STORED_PATH if files else "",
                    "bookFiles": files,
                },
            )
            return
        if parsed.path == "/api/v1/queue":
            self.send_json(
                200,
                {
                    "items": list(state.get("queue", [])),
                    "partial": bool(state.get("partial", False)),
                },
            )
            return
        if parsed.path == "/api/v1/queue/manual-import/reassign/preview":
            params = parse_qs(parsed.query)
            source = (params.get("path") or [""])[0]
            target = int((params.get("targetBookId") or ["0"])[0])
            file_format = (params.get("format") or [""])[0]
            state["previewAttempts"] = int(state.get("previewAttempts", 0)) + 1
            save_state(state)
            if source != STORED_PATH or target != BOOK_ID or file_format != "ebook":
                self.send_json(409, {"error": "unexpected reassignment preview"})
                return
            self.send_json(
                200,
                {
                    "source": STORED_PATH,
                    "destination": STORED_PATH,
                    "format": "ebook",
                    "status": "noop",
                },
            )
            return
        self.send_json(404, {"error": "not found", "path": parsed.path})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/v1/setting/"):
            self.send_json(404, {"error": "not found"})
            return
        key = unquote(parsed.path.split("/setting/", 1)[1])
        payload = self.read_json()
        state = load_state()
        settings_map = dict(state.get("settings") or {})
        settings_map[key] = payload.get("value")
        state["settings"] = settings_map
        state["settingWrites"] = int(state.get("settingWrites", 0)) + 1
        save_state(state)
        self.send_json(200, {"key": key, "value": settings_map[key]})

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
        if remove_from_client != "false" or delete_files != "false":
            self.send_json(409, {"error": "unsafe queue deletion flags"})
            return
        state["queue"] = [
            item
            for item in state.get("queue", [])
            if str(item.get("id") or "") != str(queue_id)
        ]
        state["deleteSuccesses"] = int(state.get("deleteSuccesses", 0)) + 1
        save_state(state)
        self.send_json(204)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/v1/queue/manual-import/reassign":
            self.send_json(404, {"error": "not found"})
            return
        payload = self.read_json()
        state = load_state()
        state["reassignAttempts"] = int(state.get("reassignAttempts", 0)) + 1
        save_state(state)

        if int(state.get("reassignStatus", 200)) >= 400:
            self.send_json(int(state["reassignStatus"]), {"error": "injected reassignment failure"})
            return
        if (
            str(payload.get("path") or "") != STORED_PATH
            or int(payload.get("targetBookId") or 0) != BOOK_ID
            or str(payload.get("format") or "") != "ebook"
        ):
            self.send_json(409, {"error": "unexpected reassignment request"})
            return
        if str((state.get("settings") or {}).get("import.mode") or "").casefold() != "auto":
            self.send_json(409, {"error": "reassignment requires temporary auto mode"})
            return

        set_owner(BOOK_ID)
        state = load_state()
        state["reassignSuccesses"] = int(state.get("reassignSuccesses", 0)) + 1
        save_state(state)
        self.send_json(
            200,
            {
                "ok": True,
                "source": STORED_PATH,
                "destination": STORED_PATH,
                "targetBookId": BOOK_ID,
                "format": "ebook",
            },
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

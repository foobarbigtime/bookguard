#!/usr/bin/env python3
"""Permit the two exact disposable detach requests in scheduling acceptance."""

from http.server import ThreadingHTTPServer
import os
import sqlite3
from urllib.parse import parse_qs, urlparse

from fake_bindery_unsafe_quarantine import DB, Handler as BaseHandler, load_state, save_state


BOOK_ID = 303
STORED = "/data/media/books/Second/Second Unsafe.epub"


class Handler(BaseHandler):
    def do_GET(self) -> None:
        if urlparse(self.path).path != f"/api/v1/book/{BOOK_ID}":
            super().do_GET()
            return
        with sqlite3.connect(DB) as conn:
            row = conn.execute(
                "SELECT id FROM book_files WHERE id=9003 AND book_id=? AND path=?",
                (BOOK_ID, STORED),
            ).fetchone()
        files = [{"id": 9003, "format": "ebook", "path": STORED}] if row else []
        self.send_json(200, {
            "id": BOOK_ID, "title": "Second Unsafe", "authorName": "Conflict Author",
            "ebookFilePath": STORED if files else "", "bookFiles": files,
        })

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != f"/api/v1/book/{BOOK_ID}/file":
            super().do_DELETE()
            return
        state = load_state()
        state["deregisterAttempts"] = int(state.get("deregisterAttempts", 0)) + 1
        save_state(state)
        if (parse_qs(parsed.query).get("path") or [""])[0] != STORED:
            self.send_json(409, {"error": "wrong second tracked path"})
            return
        with sqlite3.connect(DB) as conn:
            cursor = conn.execute(
                "DELETE FROM book_files WHERE id=9003 AND book_id=? AND format='ebook' AND path=?",
                (BOOK_ID, STORED),
            )
            if cursor.rowcount != 1:
                self.send_json(409, {"error": "second exact association absent"})
                return
        state = load_state()
        state["deregisterSuccesses"] = int(state.get("deregisterSuccesses", 0)) + 1
        save_state(state)
        self.send_json(200, {"ok": True})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8787"))), Handler).serve_forever()

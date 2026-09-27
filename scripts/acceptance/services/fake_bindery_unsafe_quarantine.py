#!/usr/bin/env python3
"""Permit only the exact disposable deregistration; reject all other mutations."""

from http.server import ThreadingHTTPServer
import os
import sqlite3
from urllib.parse import parse_qs, urlparse

from fake_bindery_registration_conflict import (
    BOOK_ID, DB, STORED_PATH, Handler as BaseHandler, load_state, save_state,
)


class Handler(BaseHandler):
    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != f"/api/v1/book/{BOOK_ID}/file":
            self.send_json(409, {"error": "only exact disposable file deregistration is allowed"})
            return
        state = load_state()
        state["deregisterAttempts"] = int(state.get("deregisterAttempts", 0)) + 1
        save_state(state)
        if (parse_qs(parsed.query).get("path") or [""])[0] != STORED_PATH:
            self.send_json(409, {"error": "wrong tracked path"})
            return
        with sqlite3.connect(DB) as conn:
            cursor = conn.execute(
                "DELETE FROM book_files WHERE id=9001 AND book_id=? AND format='ebook' AND path=?",
                (BOOK_ID, STORED_PATH),
            )
            if cursor.rowcount != 1:
                self.send_json(409, {"error": "exact association absent"})
                return
        state = load_state()
        state["deregisterSuccesses"] = int(state.get("deregisterSuccesses", 0)) + 1
        save_state(state)
        self.send_json(200, {"ok": True})

    def do_POST(self) -> None:
        self.send_json(409, {"error": "no scans, grabs, or other mutations permitted"})

    def do_PUT(self) -> None:
        self.send_json(409, {"error": "no setting writes permitted"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8787"))), Handler).serve_forever()

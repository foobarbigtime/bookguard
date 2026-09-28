#!/usr/bin/env python3
"""Read-only disposable release search after one exact quarantine."""

from http.server import ThreadingHTTPServer
import os
from urllib.parse import urlparse

from fake_bindery_unsafe_quarantine import Handler as BaseHandler, load_state, save_state


class Handler(BaseHandler):
    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/v1/history":
            self.send_json(200, {"items": [], "partial": False})
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        state = load_state()
        if path == "/api/v1/book/101/search":
            state["searchAttempts"] = int(state.get("searchAttempts", 0)) + 1
            state.setdefault("grabAttempts", 0)
            save_state(state)
            self.send_json(200, {
                "results": [{
                    "guid": "quarantine-alternate-guid",
                    "title": "Conflict Author - Conflict Fixture retail epub",
                    "mediaType": "ebook", "protocol": "usenet",
                    "nzbUrl": "https://indexer.invalid/disposable-release.nzb",
                    "size": 1024, "indexerName": "Disposable Indexer",
                    "approved": True,
                }],
                "partial": False,
            })
            return
        if path == "/api/v1/queue/grab":
            state["grabAttempts"] = int(state.get("grabAttempts", 0)) + 1
            save_state(state)
        self.send_json(409, {"error": "no replacement grab or scan is allowed"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8787"))), Handler).serve_forever()

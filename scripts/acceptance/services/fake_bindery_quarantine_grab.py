#!/usr/bin/env python3
"""Accept one exact disposable selected release; reject scans and other grabs."""

from http.server import ThreadingHTTPServer
import os
from urllib.parse import urlparse

from fake_bindery_quarantine_selection import Handler as BaseHandler, load_state, save_state


class Handler(BaseHandler):
    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/v1/queue/grab":
            super().do_POST()
            return
        state = load_state()
        state["grabAttempts"] = int(state.get("grabAttempts", 0)) + 1
        payload = self.read_json()
        expected = {
            "bookId": 101,
            "guid": "quarantine-alternate-guid",
            "title": "Conflict Author - Conflict Fixture retail epub",
            "protocol": "usenet", "mediaType": "ebook",
            "nzbUrl": "https://indexer.invalid/disposable-release.nzb",
            "size": 1024,
        }
        if state["grabAttempts"] != 1 or payload != expected or state.get("queue"):
            save_state(state)
            self.send_json(409, {"error": "only one exact disposable grab is allowed"})
            return
        state["queue"] = [{
            "id": 77, "bookId": 101, "title": expected["title"],
            "protocol": "usenet", "status": "downloading",
        }]
        save_state(state)
        self.send_json(200, {"queueItem": {"id": 77}})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8787"))), Handler).serve_forever()

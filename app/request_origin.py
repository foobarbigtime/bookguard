from __future__ import annotations

import os
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
_JSON_TYPES = frozenset({"application/json"})


class SameOriginMiddleware:
    """Reject cross-site state-changing requests.

    BookGuard authenticates with HTTP Basic, which browsers resend automatically
    on cross-site requests. Every non-safe method therefore must:

    * not be marked cross-site by the browser (``Sec-Fetch-Site``), and
    * carry an ``Origin`` matching the request host (or a configured trusted
      origin) when an ``Origin`` header is present, and
    * declare ``application/json`` when it has a body, so HTML forms and
      ``no-cors`` ``text/plain`` requests cannot reach the JSON handlers.

    Non-browser clients (curl, scripts) that send neither header are unaffected.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        reason = rejection_reason(_headers(scope), scheme=scope.get("scheme", "http"))
        if reason:
            response = JSONResponse({"detail": reason}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _headers(scope: Scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in scope.get("headers", []):
        headers[name.decode("latin-1").lower()] = value.decode("latin-1")
    return headers


def trusted_origins() -> set[str]:
    raw = os.getenv("BOOKGUARD_TRUSTED_ORIGINS", "")
    return {
        item.strip().rstrip("/").lower()
        for item in raw.replace("\n", ",").split(",")
        if item.strip()
    }


def rejection_reason(headers: dict[str, str], *, scheme: str = "http") -> str | None:
    fetch_site = headers.get("sec-fetch-site", "").strip().lower()
    if fetch_site and fetch_site not in _ALLOWED_FETCH_SITES:
        return "Cross-site requests are not allowed."

    origin = headers.get("origin", "").strip()
    if origin:
        normalized = origin.rstrip("/").lower()
        if normalized == "null":
            return "Cross-site requests are not allowed."
        host = headers.get("host", "").strip().lower()
        parsed = urlsplit(normalized)
        same_origin = (
            parsed.scheme == scheme and parsed.netloc == host
            and not parsed.path and not parsed.query and not parsed.fragment
        )
        if not same_origin and normalized not in trusted_origins():
            return "Cross-site requests are not allowed."

    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    has_body = bool(content_type) or headers.get("content-length", "0").strip() not in {"", "0"} \
        or "transfer-encoding" in headers
    if has_body and content_type not in _JSON_TYPES:
        return "State-changing requests must use Content-Type: application/json."
    return None

from __future__ import annotations

import base64
import binascii
import secrets

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import ConfigurationError, load_auth_settings


class BasicAuthMiddleware:
    """Require configured HTTP Basic credentials for every route except health."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return

        try:
            configured = load_auth_settings()
        except ConfigurationError as exc:
            response = JSONResponse(
                {"detail": str(exc)},
                status_code=503,
            )
            await response(scope, receive, send)
            return

        authorization = next(
            (
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"authorization"
            ),
            b"",
        )
        if not _credentials_match(authorization, configured.username, configured.password):
            response = JSONResponse(
                {"detail": "Authentication required."},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="BookGuard", charset="UTF-8"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _credentials_match(authorization: bytes, username: str, password: str) -> bool:
    try:
        scheme, encoded = authorization.split(b" ", 1)
        if scheme.lower() != b"basic":
            return False
        supplied = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False

    expected = f"{username}:{password}"
    return secrets.compare_digest(supplied, expected)

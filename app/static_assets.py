"""Versioned URLs for BookGuard's stylesheet and scripts.

Browsers keep a stylesheet or script they have already fetched and may reuse
it without asking the server again, so after an upgrade a page could pair new
HTML with the previous release's stylesheet. Each asset URL therefore carries a
short hash of the file's bytes: a changed file gets a new URL, an unchanged one
stays cached. Static responses also say ``no-cache``, so a browser revalidates
(a cheap 304 when nothing changed) instead of assuming its copy is current.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
from pathlib import Path

from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path("static")


@lru_cache(maxsize=None)
def _digest(name: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def static_url(name: str) -> str:
    """The URL for one file under static/, versioned by its content."""
    digest = _digest(name)
    return f"/static/{name}?v={digest}" if digest else f"/static/{name}"


class RevalidatedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import requests

from .config import settings


def _validated_tika_url() -> tuple[str, str]:
    """Return the deployment-configured Tika base URL when it is safe to use."""
    url = str(settings.verification_tika_url or "").strip().rstrip("/")
    if not url:
        return "", ""
    if any(character.isspace() for character in url):
        return "", "Tika URL contains whitespace."

    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError:
        return "", "Tika URL is invalid."

    if parsed.scheme not in {"http", "https"}:
        return "", "Tika URL must use http or https."
    if not parsed.hostname:
        return "", "Tika URL must include a hostname."
    if parsed.username is not None or parsed.password is not None:
        return "", "Tika URL must not contain credentials."
    if parsed.query or parsed.fragment:
        return "", "Tika URL must not contain a query string or fragment."
    return url, ""


def extract_text(path: str) -> tuple[str, str]:
    """Return text from Tika, or an empty result with a diagnostic message."""
    if not settings.verification_use_tika:
        return "", ""

    url, url_error = _validated_tika_url()
    if url_error:
        return "", url_error
    if not url:
        return "", ""

    try:
        with Path(path).open("rb") as handle:
            response = requests.put(
                f"{url}/tika",
                data=handle,
                headers={"Accept": "text/plain"},
                timeout=90,
                allow_redirects=False,
            )
        if response.status_code >= 300:
            return "", f"Tika returned HTTP {response.status_code}."
        return response.text[: settings.verification_max_text_chars], ""
    except (OSError, requests.RequestException) as exc:
        return "", f"Tika unavailable: {exc}"


def test_connection() -> dict:
    raw_url = str(settings.verification_tika_url or "").strip()
    if not raw_url:
        return {
            "configured": False,
            "ok": False,
            "message": "Tika URL is not configured.",
        }

    url, url_error = _validated_tika_url()
    if url_error:
        return {
            "configured": True,
            "ok": False,
            "message": url_error,
        }

    try:
        response = requests.get(
            f"{url}/version",
            timeout=10,
            allow_redirects=False,
        )
        if response.status_code >= 300:
            return {
                "configured": True,
                "ok": False,
                "message": f"Tika returned HTTP {response.status_code}.",
            }
        return {
            "configured": True,
            "ok": True,
            "message": response.text.strip()[:200] or "Tika responded successfully.",
        }
    except requests.RequestException as exc:
        return {"configured": True, "ok": False, "message": str(exc)[:300]}

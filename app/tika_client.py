from __future__ import annotations

from pathlib import Path

import requests

from .config import settings


def extract_text(path: str) -> tuple[str, str]:
    """Return text from Tika, or an empty result with a diagnostic message."""
    url = str(settings.verification_tika_url or "").rstrip("/")
    if not settings.verification_use_tika or not url:
        return "", ""

    try:
        with Path(path).open("rb") as handle:
            response = requests.put(
                f"{url}/tika",
                data=handle,
                headers={"Accept": "text/plain"},
                timeout=90,
            )
        if response.status_code >= 300:
            return "", f"Tika returned HTTP {response.status_code}."
        return response.text[: settings.verification_max_text_chars], ""
    except (OSError, requests.RequestException) as exc:
        return "", f"Tika unavailable: {exc}"


def test_connection() -> dict:
    url = str(settings.verification_tika_url or "").rstrip("/")
    if not url:
        return {
            "configured": False,
            "ok": False,
            "message": "Tika URL is not configured.",
        }

    try:
        response = requests.get(f"{url}/version", timeout=10)
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

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from .config import settings


class BinderyClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class CandidateDecision:
    safe: bool
    reason: str


def _tokens(value: object) -> set[str]:
    text = str(value or "").casefold()
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text)
    return {part for part in cleaned.split() if len(part) > 1}


def evaluate_replacement_candidate(
    result: dict[str, Any],
    *,
    expected_title: str,
    expected_author: str,
) -> CandidateDecision:
    """Conservative automatic-grab gate layered on top of Bindery.

    Bindery blocklists by release GUID, so the same bad release can still
    appear from a second indexer under a different GUID. Automatic mode must
    therefore independently require evidence for the expected title/author.
    """
    if result.get("approved") is False:
        return CandidateDecision(False, str(result.get("rejection") or "Bindery rejected release"))

    release_text = " ".join(
        str(result.get(key) or "")
        for key in ("title", "author", "bookTitle")
    )
    release_tokens = _tokens(release_text)
    title_tokens = _tokens(expected_title)
    author_tokens = _tokens(expected_author)

    if not title_tokens:
        return CandidateDecision(False, "Expected title is unavailable")

    shared_title = release_tokens & title_tokens
    required_title = 1 if len(title_tokens) <= 2 else 2
    if len(shared_title) < required_title:
        return CandidateDecision(False, "Release does not contain enough expected title evidence")

    if author_tokens:
        surname = str(expected_author or "").strip().split()[-1].casefold()
        if surname and surname not in release_tokens:
            return CandidateDecision(False, "Release does not contain the expected author surname")

    return CandidateDecision(True, "Release contains expected title and author evidence")


class BinderyClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: int = 45):
        self.base_url = (base_url if base_url is not None else settings.bindery_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.bindery_api_key
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.base_url:
            raise BinderyClientError("Bindery URL is not configured")
        if not self.api_key:
            raise BinderyClientError("Bindery API key is not configured")

        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-Api-Key"] = self.api_key
        headers.setdefault("Accept", "application/json")

        try:
            response = requests.request(
                method,
                f"{self.base_url}/api/v1{path}",
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise BinderyClientError(f"Bindery request failed: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text.strip()[:500]
            raise BinderyClientError(
                f"Bindery {method.upper()} {path} returned HTTP {response.status_code}: {detail}"
            )

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise BinderyClientError("Bindery returned a non-JSON response") from exc

    def system_status(self) -> dict[str, Any]:
        return self._request("GET", "/system/status")

    def get_book(self, book_id: int) -> dict[str, Any]:
        return self._request("GET", f"/book/{int(book_id)}")

    def search_book(self, book_id: int) -> dict[str, Any]:
        return self._request("POST", f"/book/{int(book_id)}/search")

    def deregister_file(self, book_id: int, tracked_path: str) -> dict[str, Any]:
        if not str(tracked_path or "").strip():
            raise BinderyClientError("Tracked path is required")
        return self._request(
            "DELETE",
            f"/book/{int(book_id)}/file",
            params={"path": tracked_path},
        )

    def blocklist_history(self, history_id: int) -> dict[str, Any]:
        return self._request("POST", f"/history/{int(history_id)}/blocklist")

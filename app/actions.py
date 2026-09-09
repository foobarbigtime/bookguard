from __future__ import annotations

import os
from pathlib import Path
import shutil
from urllib.parse import quote

import requests

from .config import settings
from .db import associations_inside_path


class ActionError(RuntimeError):
    pass


def _require_actions() -> None:
    if not settings.allow_actions:
        raise ActionError("Actions are disabled. Set BOOKGUARD_ALLOW_ACTIONS=true to enable them.")
    if not settings.bindery_api_key:
        raise ActionError("BINDERY_API_KEY is required for Bindery actions.")


def detach(book_id: int, stored_path: str) -> None:
    _require_actions()
    url = f"{settings.bindery_url}/api/v1/book/{book_id}/file?path={quote(stored_path, safe='')}"
    response = requests.delete(url, headers={"X-Api-Key": settings.bindery_api_key}, timeout=30)
    if response.status_code >= 300:
        raise ActionError(f"Bindery detach failed ({response.status_code}): {response.text[:500]}")


def _safe_quarantine_destination(local_path: str) -> str:
    source = Path(local_path).resolve()
    allowed = [Path(settings.audiobook_root).resolve(), Path(settings.ebook_root).resolve()]
    if not any(source == root or root in source.parents for root in allowed):
        raise ActionError("Refusing to quarantine a path outside configured media roots.")

    qroot = Path(settings.quarantine_root).resolve()
    qroot.mkdir(parents=True, exist_ok=True)
    dest = qroot / source.name
    if dest.exists():
        stem = source.stem
        suffix = source.suffix
        i = 2
        while True:
            candidate = qroot / f"{stem}-{i}{suffix}"
            if not candidate.exists():
                dest = candidate
                break
            i += 1
    return str(dest)


def quarantine(book_id: int, stored_path: str, local_path: str) -> str:
    _require_actions()
    if not os.path.exists(local_path):
        raise ActionError("The physical path is already missing.")

    linked = associations_inside_path(stored_path)
    if len(linked) != 1:
        raise ActionError(
            f"Refusing to move a shared path: Bindery has {len(linked)} associations inside it."
        )

    destination = _safe_quarantine_destination(local_path)
    detach(book_id, stored_path)
    try:
        shutil.move(local_path, destination)
    except Exception as exc:
        raise ActionError(
            "Bindery was detached, but moving the file/folder failed. Manual attention is required: "
            f"{exc}"
        ) from exc
    return destination

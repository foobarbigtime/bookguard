"""How a misfiled ebook relates to the catalogued book it really is.

When ISBN and title evidence show a file is really another Bindery book (the
classifier's ``actualBook``), the useful question is what that means for the
operator:

- ``duplicate_identical``: the real book already has a byte-identical ebook, so
  this copy adds nothing.
- ``duplicate_edition``: the real book already has its own ebook, which passed
  the latest library scan, so this copy is a redundant edition.
- ``duplicate_unconfirmed``: the real book has its own ebook, but that file has
  not passed the scan yet; check it before removing this copy.
- ``missing_ebook``: the real book has no ebook file, so this file is very
  likely its missing ebook and could be re-assigned to it.

Everything here is read-only: it maps Bindery paths into BookGuard's read-only
library view with the same rules as the hard-link tooling (no parent
traversal, no symbolic links, must stay inside the library) and compares
fingerprints. Nothing is moved, deleted, or re-assigned.
"""

from __future__ import annotations

from pathlib import Path
import stat
from typing import Callable

from .config import settings
from .file_safety import sha256_file

MAX_OWNER_FILES = 3


def library_path(bindery_path: str) -> Path | None:
    """Map a Bindery ebook path into the read-only library view, or None."""
    try:
        relative = Path(bindery_path).relative_to(Path(settings.ebook_bindery_prefix))
    except ValueError:
        return None
    if not relative.parts or ".." in relative.parts:
        return None
    try:
        root = Path(settings.ebook_root).resolve(strict=True)
    except OSError:
        return None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
        if not stat.S_ISREG(resolved.stat().st_mode):
            return None
    except (OSError, ValueError):
        return None
    return resolved


def classify_relationship(
    own_sha256: str,
    owner: dict,
    status_for_book: Callable[[int], str],
) -> dict:
    """Classify a misfiled file against the real book's own ebook files."""
    book_id = int(owner.get("bookId") or 0)
    title = str(owner.get("title") or "")
    files = list(owner.get("ebookFiles") or [])[:MAX_OWNER_FILES]
    result: dict = {"bookId": book_id, "title": title, "ownerFiles": []}

    if not files:
        result["relationship"] = "missing_ebook"
        result["explanation"] = (
            f"\"{title}\" (Bindery book {book_id}) has no ebook file. This file is very "
            "likely its missing ebook and could be re-assigned to it."
        )
        return result

    max_bytes = settings.verification_snapshot_max_bytes
    for entry in files:
        path = library_path(str(entry.get("path") or ""))
        record = {"path": str(entry.get("path") or ""), "readable": path is not None}
        if path is not None:
            try:
                if path.stat().st_size <= max_bytes:
                    record["identical"] = sha256_file(path) == own_sha256
            except OSError:
                record["readable"] = False
        result["ownerFiles"].append(record)
        if record.get("identical"):
            result["relationship"] = "duplicate_identical"
            result["explanation"] = (
                f"This file is a byte-identical copy of the ebook \"{title}\" (Bindery book "
                f"{book_id}) already has. Removing this copy loses nothing."
            )
            return result

    status = str(status_for_book(book_id) or "")
    result["ownerScanStatus"] = status
    if status == "PASS":
        result["relationship"] = "duplicate_edition"
        result["explanation"] = (
            f"\"{title}\" (Bindery book {book_id}) already has its own ebook, which passed the "
            "library scan. This file is another edition of it and is redundant."
        )
    else:
        result["relationship"] = "duplicate_unconfirmed"
        result["explanation"] = (
            f"\"{title}\" (Bindery book {book_id}) already has its own ebook, but that file has "
            "not passed the library scan yet. Check it before removing this copy."
        )
    return result


def latest_ebook_scan_status(book_id: int) -> str:
    """The latest scan's classification for a book's ebook ('' if unknown)."""
    from .db import local_conn

    with local_conn() as conn:
        row = conn.execute(
            """
            SELECT r.classification
            FROM scan_results r
            WHERE r.scan_id = (SELECT id FROM scans ORDER BY started_at DESC LIMIT 1)
              AND r.book_id = ? AND r.format = 'ebook'
            LIMIT 1
            """,
            (int(book_id),),
        ).fetchone()
    return str(row["classification"]) if row else ""

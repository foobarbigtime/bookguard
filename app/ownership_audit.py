"""Read-only detection of nested cross-book Bindery ownership."""

from collections import defaultdict
from pathlib import PurePosixPath

from .db import bindery_conn


def _ancestors(path: str) -> tuple[str, ...]:
    """Return component-aware ancestors of an absolute stored path."""
    if not path.startswith("/") or ".." in path.split("/"):
        raise ValueError("Invalid Bindery path")

    parsed = PurePosixPath(path)
    return tuple(
        str(parent)
        for parent in parsed.parents
        if str(parent) != "/"
    )


def audit_ownership() -> list[dict]:
    """Find paths nested under directories owned by other books.

    Reads Bindery only. Does not modify media or database records.
    """
    with bindery_conn() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT
                    bf.id AS file_id,
                    bf.book_id,
                    bf.path,
                    b.title
                FROM book_files bf
                JOIN books b ON b.id = bf.book_id
                WHERE bf.path IS NOT NULL
                ORDER BY bf.id
                """
            )
        ]

    paths = defaultdict(list)

    for row in rows:
        path = str(row["path"])
        paths[str(PurePosixPath(path))].append(row)

    conflicts = []

    for child in rows:
        child_path = str(child["path"])

        for ancestor in _ancestors(child_path):
            for parent in paths.get(ancestor, []):
                if parent["book_id"] == child["book_id"]:
                    continue

                conflicts.append({
                    "parent_file_id": parent["file_id"],
                    "parent_book_id": parent["book_id"],
                    "parent_title": parent["title"],
                    "parent_path": parent["path"],
                    "child_file_id": child["file_id"],
                    "child_book_id": child["book_id"],
                    "child_title": child["title"],
                    "child_path": child["path"],
                })

    return conflicts

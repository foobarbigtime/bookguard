"""Open library items grouped by the decision they need (Home and Review).

Every REVIEW or REJECT result of the latest scan that the operator has not
resolved is paired with its newest conclusive verification and placed in one
group, so Home can say "23 books contain the wrong file" and Review can list
them with a plain sentence and a suggestion.

Read-only: nothing is verified, re-scanned, or changed here. A verification
that only recorded a scanner outage proves nothing about the file, so it is
ignored and the item stays undecided.
"""

from __future__ import annotations

import json
from typing import Any

from .db import latest_results, local_conn
from .language_detection import declared_language
from .triage import triage_state
from .verification_status import verification_is_inconclusive

# key, label, tone; ordered by how urgently they need a decision.
GROUPS: list[tuple[str, str, str]] = [
    ("unsafe", "Damaged or unsafe files", "red"),
    ("move", "Filed under the wrong book", "amber"),
    ("duplicate", "Copies of other books", "amber"),
    ("wrong", "Contain the wrong file", "amber"),
    ("metadata", "Right book, wrong details", "blue"),
    ("undecided", "Undecided", "grey"),
    ("verified", "Checked and correct", "green"),
]
GROUP_LABELS = {key: label for key, label, _ in GROUPS}

_DUPLICATE_RELATIONSHIPS = {"duplicate_identical", "duplicate_edition", "duplicate_unconfirmed"}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def latest_verifications(result_ids: list[int]) -> dict[int, dict[str, Any]]:
    """The newest conclusive verification for each result, without touching files."""
    if not result_ids:
        return {}
    latest: dict[int, dict[str, Any]] = {}
    with local_conn() as conn:
        if not _table_exists(conn, "content_verifications"):
            return {}
        for start in range(0, len(result_ids), 500):
            chunk = result_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT id, result_id, verdict, confidence, source, updated_at, evidence_json
                FROM content_verifications
                WHERE result_id IN ({placeholders})
                ORDER BY result_id, updated_at DESC, id DESC
                """,
                chunk,
            ).fetchall()
            for row in rows:
                result_id = int(row["result_id"])
                if result_id in latest:
                    continue
                item = dict(row)
                try:
                    item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
                except (TypeError, ValueError):
                    item["evidence"] = {}
                latest[result_id] = item
    return {
        result_id: item
        for result_id, item in latest.items()
        if not verification_is_inconclusive(item.get("verdict"), item.get("evidence"))
    }


def _group_for(verification: dict | None) -> str:
    if not verification:
        return "undecided"
    verdict = str(verification.get("verdict") or "")
    relationship = str(((verification.get("evidence") or {}).get("catalogue") or {}).get("relationship") or "")
    if verdict == "UNSAFE_FILE":
        return "unsafe"
    if verdict == "WRONG_CONTENT" and relationship == "missing_ebook":
        return "move"
    if verdict == "WRONG_CONTENT" and relationship in _DUPLICATE_RELATIONSHIPS:
        return "duplicate"
    if verdict in {"WRONG_CONTENT", "WRONG_MEDIA_TYPE"}:
        return "wrong"
    if verdict == "METADATA_ERROR":
        return "metadata"
    if verdict == "VERIFIED_CORRECT":
        return "verified"
    return "undecided"


def _by(author: str) -> str:
    return f" by {author}" if author else ""


def _contains(row: dict, verification: dict | None) -> str:
    """What the file actually is, as far as BookGuard knows."""
    evidence = (verification or {}).get("evidence") or {}
    catalogue = evidence.get("catalogue") or {}
    embedded = evidence.get("embedded") or {}
    identity = evidence.get("identity") or {}
    metadata = row.get("metadata") or {}
    title = (
        str(catalogue.get("title") or "")
        or str(embedded.get("title") or "")
        or str(identity.get("detected_title") or "")
        or str(metadata.get("detected_title") or metadata.get("title") or "")
    )
    author = (
        str(embedded.get("author") or "")
        or str(identity.get("detected_author") or "")
        or str(metadata.get("detected_author") or metadata.get("author") or "")
    )
    return f"{title}{_by(author)}" if title else ""


def _suggestion(group: str, verification: dict | None) -> str:
    evidence = (verification or {}).get("evidence") or {}
    catalogue = evidence.get("catalogue") or {}
    other = str(catalogue.get("title") or "the other book")
    relationship = str(catalogue.get("relationship") or "")
    if group == "unsafe":
        return "Quarantine it. Quarantine moves the file out of the library; nothing is deleted."
    if group == "move":
        return f"Move it to “{other}”, which has no ebook. Bindery moves the file; nothing is downloaded."
    if group == "duplicate":
        if relationship == "duplicate_identical":
            return f"It is an identical copy of “{other}”’s ebook. Removing this copy loses nothing."
        if relationship == "duplicate_edition":
            return f"“{other}” already has its own ebook, so this copy is redundant."
        return f"“{other}” has its own ebook, but that file has not passed a check yet. Check it before removing this copy."
    if group == "wrong":
        if (verification or {}).get("verdict") == "WRONG_MEDIA_TYPE":
            return "The file is the wrong kind of media for this entry. Replace it with the right book."
        return "Replace it with the right book."
    if group == "metadata":
        return "Fix the book’s details. BookGuard checks the file again before writing anything."
    if group == "verified":
        return "Mark it reviewed."
    if verification:
        return "BookGuard could not prove what this file is. Check it yourself, then mark it reviewed."
    return "Verify it to see what the file really contains."


def review_item(row: dict, verification: dict | None) -> dict[str, Any]:
    group = _group_for(verification)
    evidence = (verification or {}).get("evidence") or {}
    return {
        "id": int(row["id"]),
        "bookId": int(row.get("book_id") or 0),
        "title": str(row.get("title") or ""),
        "author": str(row.get("author") or ""),
        "format": str(row.get("format") or ""),
        "classification": str(row.get("classification") or ""),
        "reasonCode": str(row.get("reason_code") or "UNKNOWN"),
        "riskScore": int(row.get("risk_score") or 0),
        "storedPath": str(row.get("stored_path") or ""),
        "language": declared_language(row),
        "scanReasons": list(row.get("reasons") or []),
        "group": group,
        "groupLabel": GROUP_LABELS[group],
        "contains": _contains(row, verification),
        "suggestion": _suggestion(group, verification),
        "verified": verification is not None,
        "verificationId": int((verification or {}).get("id") or 0),
        "verdict": str((verification or {}).get("verdict") or ""),
        "confidence": int((verification or {}).get("confidence") or 0),
        "verifiedAt": str((verification or {}).get("updated_at") or ""),
        "explanation": str(evidence.get("explanation") or ""),
        "relationship": str((evidence.get("catalogue") or {}).get("relationship") or ""),
        "otherBook": {
            "id": int((evidence.get("catalogue") or {}).get("bookId") or 0),
            "title": str((evidence.get("catalogue") or {}).get("title") or ""),
        },
    }


def open_review_items() -> list[dict[str, Any]]:
    """Unresolved REVIEW and REJECT items of the latest scan, most urgent group first."""
    rows = [
        row
        for classification in ("REJECT", "REVIEW")
        for row in latest_results(classification=classification, limit=10000)
        if not triage_state(row)["resolved"]
    ]
    verifications = latest_verifications([int(row["id"]) for row in rows])
    items = [review_item(row, verifications.get(int(row["id"]))) for row in rows]
    order = {key: index for index, (key, _, _) in enumerate(GROUPS)}
    items.sort(key=lambda item: (order[item["group"]], -item["riskScore"], item["author"].casefold(), item["title"].casefold()))
    return items


def group_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key, _, _ in GROUPS}
    for item in items:
        counts[item["group"]] += 1
    return counts

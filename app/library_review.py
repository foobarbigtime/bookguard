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

from .db import latest_review_results, local_conn
from .language_detection import declared_language
from .triage import triage_states
from .verification_status import verification_is_inconclusive

# key, label, tone; ordered by how urgently they need a decision.
GROUPS: list[tuple[str, str, str]] = [
    ("unsafe", "Damaged or unsafe files", "red"),
    ("move", "Filed under the wrong book", "amber"),
    ("duplicate", "Copies of other books", "amber"),
    ("wrong", "Contain the wrong file", "amber"),
    ("language", "Not in your library languages", "amber"),
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


def _newest_verifications(result_ids: list[int]) -> dict[int, dict[str, Any]]:
    """The newest verification record for each result, conclusive or not."""
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
    return latest


def split_verifications(result_ids: list[int]) -> tuple[dict[int, dict[str, Any]], set[int]]:
    """(newest conclusive verification per result, results whose newest one was a scanner outage).

    One read serves both: a malware scan that gave no verdict proves nothing about the file."""
    conclusive: dict[int, dict[str, Any]] = {}
    outages: set[int] = set()
    for result_id, item in _newest_verifications(result_ids).items():
        if verification_is_inconclusive(item.get("verdict"), item.get("evidence")):
            outages.add(result_id)
        else:
            conclusive[result_id] = item
    return conclusive, outages


def latest_verifications(result_ids: list[int]) -> dict[int, dict[str, Any]]:
    """The newest conclusive verification for each result, without touching files."""
    return split_verifications(result_ids)[0]


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


def _identity(evidence: dict) -> dict[str, Any]:
    """What Bindery expects next to what the file says about itself."""
    expected = evidence.get("expected") or {}
    embedded = evidence.get("embedded") or {}
    content = evidence.get("content") or {}
    expected_signal = content.get("expected_signal") or {}
    embedded_signal = content.get("embedded_signal") or {}
    return {
        "expectedTitle": str(expected.get("title") or ""),
        "expectedAuthor": str(expected.get("author") or ""),
        "fileTitle": str(embedded.get("title") or ""),
        "fileAuthor": str(embedded.get("author") or ""),
        "isbns": [str(i) for i in (embedded.get("identifiers") or [])][:5],
        "textCharacters": int(content.get("text_characters_examined") or 0),
        "expectedTitleInText": bool(expected_signal.get("title_found")),
        "expectedAuthorInText": bool(expected_signal.get("author_found")),
        "fileTitleInText": bool(embedded_signal.get("title_found")),
        "fileAuthorInText": bool(embedded_signal.get("author_found")),
    }


def _why_undecided(evidence: dict) -> str:
    """A specific plain-language reason BookGuard could not decide, from the stored evidence."""
    if (evidence.get("reasonCode") or "") == "TITLE_DESIGNATION_CONFLICT":
        return str(evidence.get("explanation") or "")
    who = _identity(evidence)
    if not who["expectedTitle"] and not who["fileTitle"]:
        return "BookGuard could not prove what this file is."
    if who["textCharacters"] == 0:
        return ("No readable text could be extracted from this file, so its content could not be "
                "compared with the book. It may be image-only, protected, or damaged.")
    parts = []
    if who["fileTitle"] or who["fileAuthor"]:
        parts.append(f"The file calls itself “{who['fileTitle'] or 'untitled'}”{_by(who['fileAuthor'])}; "
                     f"Bindery has it as “{who['expectedTitle']}”{_by(who['expectedAuthor'])}.")
    else:
        parts.append("The file carries no title or author of its own.")
    title_in, author_in = who["expectedTitleInText"], who["expectedAuthorInText"]
    if title_in and author_in:
        parts.append("Both the title and the author appear in the book, but not in a place that proves "
                     "they identify it rather than a series list or advertisement.")
    elif title_in:
        parts.append(f"The title appears in the book but the author “{who['expectedAuthor']}” does not; "
                     "Bindery’s author name may be spelled differently from the book’s.")
    elif author_in:
        parts.append(f"The author appears in the book but the title “{who['expectedTitle']}” does not; "
                     "it may be a different title, edition or translation.")
    elif who["fileTitleInText"] or who["fileAuthorInText"]:
        parts.append("Neither Bindery’s title nor its author appears in the book, but the file’s own title "
                     "does, so this may be a different book.")
    else:
        parts.append("Neither the title nor the author could be found in the readable text.")
    for step in evidence.get("secondPass") or []:
        if step.get("check") == "catalogueSearch":
            count = int(step.get("matches") or 0)
            parts.append("BookGuard also searched your Bindery library for a book matching the file’s own "
                         + ("title and author and found none." if not count
                            else f"title and author and found {count}, too many to pick one safely."))
    return " ".join(parts)


def _suggestion(group: str, verification: dict | None, language: dict | None = None) -> str:
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
    if group == "language":
        label = str((language or {}).get("label") or "another language")
        return (f"This copy is in {label}, which is not one of your library languages. "
                "Replace it: BookGuard quarantines it (nothing is deleted) and Bindery searches for another copy, "
                "so set Bindery's preferred language too. If the book's own title in Bindery is the "
                f"{label} title, it is a {label} edition: remove it in Bindery instead.")
    if group == "metadata":
        return "Fix the book’s details. BookGuard checks the file again before writing anything."
    if group == "verified":
        return "Mark it reviewed."
    if verification:
        return "BookGuard could not prove what this file is. Check it yourself, then mark it reviewed."
    return "Verify it to see what the file really contains."


def review_item(row: dict, verification: dict | None) -> dict[str, Any]:
    group = _group_for(verification)
    language = declared_language(row)
    # A file in a language the user does not keep is flagged for removal, unless
    # it already has a more specific problem (unsafe, wrong book, copy, misfiled).
    if language["otherLanguage"] and group in {"verified", "metadata", "undecided"}:
        group = "language"
    evidence = (verification or {}).get("evidence") or {}
    # The scan can file a mixed folder under another reason (a music-tagged
    # folder is MUSIC_MISMATCH there), while verification inspects the whole
    # set. Either source marking it mixed keeps whole-folder actions secondary.
    mixed_audio = "MIXED_AUDIO_CONTENT" in {row.get("reason_code"), evidence.get("reasonCode")}
    return {
        "id": int(row["id"]),
        "bookId": int(row.get("book_id") or 0),
        "title": str(row.get("title") or ""),
        "author": str(row.get("author") or ""),
        "format": str(row.get("format") or ""),
        "classification": str(row.get("classification") or ""),
        "reasonCode": str(row.get("reason_code") or "UNKNOWN"),
        "mixedAudio": mixed_audio,
        "riskScore": int(row.get("risk_score") or 0),
        "storedPath": str(row.get("stored_path") or ""),
        "language": language,
        "scanReasons": list(row.get("reasons") or []),
        "group": group,
        "groupLabel": GROUP_LABELS[group],
        "contains": _contains(row, verification),
        "suggestion": ("This folder contains multiple works. Review its recordings before changing attachments."
                       if mixed_audio
                       else _suggestion(group, verification, language)),
        "verified": verification is not None,
        "verificationId": int((verification or {}).get("id") or 0),
        "verdict": str((verification or {}).get("verdict") or ""),
        "confidence": int((verification or {}).get("confidence") or 0),
        "verifiedAt": str((verification or {}).get("updated_at") or ""),
        "explanation": str(evidence.get("explanation") or ""),
        "identity": _identity(evidence) if verification else None,
        "whyUndecided": _why_undecided(evidence) if verification and group == "undecided" else "",
        "relationship": str((evidence.get("catalogue") or {}).get("relationship") or ""),
        "otherBook": {
            "id": int((evidence.get("catalogue") or {}).get("bookId") or 0),
            "title": str((evidence.get("catalogue") or {}).get("title") or ""),
        },
    }


def open_review_items(*, include_resolved: bool = False) -> list[dict[str, Any]]:
    """Unresolved REVIEW and REJECT items of the latest scan, most urgent group first."""
    rows = latest_review_results()
    states = triage_states(rows)
    if not include_resolved:
        rows = [row for row in rows if not states[int(row["id"])]["resolved"]]
    verifications, outages = split_verifications([int(row["id"]) for row in rows])
    items = [review_item(row, verifications.get(int(row["id"]))) for row in rows]
    for item in items:
        item["scannerOutage"] = item["id"] in outages
        item["resolved"] = states[item["id"]]["resolved"]
        item["resolution"] = states[item["id"]].get("resolution", "")
    order = {key: index for index, (key, _, _) in enumerate(GROUPS)}
    items.sort(key=lambda item: (order[item["group"]], -item["riskScore"], item["author"].casefold(), item["title"].casefold()))
    return items


def group_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key, _, _ in GROUPS}
    for item in items:
        counts[item["group"]] += 1
    return counts

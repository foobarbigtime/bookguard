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
from pathlib import Path
import re
from typing import Any

from .db import latest_review_results, local_conn
from .identity_search import PLACEHOLDER_AUTHORS
from .language_detection import declared_language
from .title_matching import normalize
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
    tags = evidence.get("identity") or {}
    expected_signal = content.get("expected_signal") or {}
    embedded_signal = content.get("embedded_signal") or {}
    # Audiobooks keep what the tracks' tags say under "identity", not under "embedded".
    audio = "wholeSet" in tags or "sampleFiles" in evidence
    whole = tags.get("wholeSet") or {}
    names = tags.get("filenameSupport") or {}
    return {
        "isAudiobook": audio,
        "expectedTitle": str(expected.get("title") or ""),
        "expectedAuthor": str(expected.get("author") or ""),
        "fileTitle": str(embedded.get("title") or (tags.get("detected_title") if audio else "") or ""),
        "fileAuthor": str(embedded.get("author") or (tags.get("detected_author") if audio else "") or ""),
        "isbns": [str(i) for i in (embedded.get("identifiers") or [])][:5],
        "textCharacters": int(content.get("text_characters_examined") or 0),
        "expectedTitleInText": bool(expected_signal.get("title_found")),
        "expectedAuthorInText": bool(expected_signal.get("author_found")),
        "fileTitleInText": bool(embedded_signal.get("title_found")),
        "fileAuthorInText": bool(embedded_signal.get("author_found")),
        "hasContentSection": "text_characters_examined" in content,
        "mixedContent": bool(whole.get("mixedContent")),
        "distinctMismatchTitles": int(whole.get("distinctMismatchTitleCount") or 0),
        "tagTitleMatches": int(whole.get("titleMatchCount") or 0),
        "tagTitleMismatches": int(whole.get("titleMismatchCount") or 0),
        "trackCount": int(whole.get("readableCount") or names.get("fileCount") or 0),
        "filenamesNameTheBook": bool(names.get("strong")),
        "notes": [str(n)[:300] for n in (evidence.get("notes") or [])][:5],
        "ocr": evidence.get("ocr") or None,
        "downloadNames": [str(r.get("raw") or "") for r in ((evidence.get("provenance") or {}).get("releases") or [])][:3],
        "notABook": [str(n) for n in ((evidence.get("provenance") or {}).get("notABook") or [])][:3],
        "proofSources": [str(n) for n in ((evidence.get("proof") or {}).get("sources") or [])],
    }

_READABLE_SUFFIXES = {".epub", ".pdf", ".txt", ".rtf", ".mobi", ".azw", ".azw3"}


_COLLECTION_TITLE = re.compile(
    r"\b\d+\s+books?\b|\bbooks?\s+\d+\s*(?:-|to|\u2013)\s*\d+\b|\bcollection\b|\bcomplete\b|\bbox(?:ed)?\s*set\b"
    r"|\bomnibus\b|\bseries\b|\bboxset\b", re.IGNORECASE)


def _why_undecided(evidence: dict, suffix: str = "", fmt: str = "ebook") -> tuple[str, str]:
    """(kind, plain-language reason) BookGuard could not decide, from the stored evidence."""
    if (evidence.get("reasonCode") or "") == "TITLE_DESIGNATION_CONFLICT":
        return "volume_conflict", str(evidence.get("explanation") or "")
    who = _identity(evidence)
    if fmt == "audiobook":
        # Audiobooks have no book text; the evidence is the audio tags of the tracks.
        title = who["expectedTitle"]
        tagged = f"“{who['fileTitle']}”{_by(who['fileAuthor'])}" if who["fileTitle"] else ""
        matches, mismatches, tracks = who["tagTitleMatches"], who["tagTitleMismatches"], who["trackCount"]
        # A collection needs the TAGS to name several works; a title like "The Complete Maus" alone is not.
        if _COLLECTION_TITLE.search(title) and (who["mixedContent"] or who["distinctMismatchTitles"] >= 2):
            return "audiobook_collection", (
                f"“{title}” looks like a set of several books under one Bindery entry, and the tracks' tags "
                "name different works, so the folder cannot be matched to a single book. Show recordings "
                "lists the separate books it contains.")
        if who["fileTitle"] and matches == 0 and mismatches > 0:
            return "audiobook_other_work", (
                f"The tracks' tags name {tagged}, not “{title}”{_by(who['expectedAuthor'])}. "
                "This looks like a different book filed under this Bindery entry.")
        if matches == 0 and mismatches == 0 and not who["fileTitle"]:
            extra = "" if who["filenamesNameTheBook"] else " and the filenames do not name the book"
            return "audiobook_no_tags", (
                f"The tracks carry no usable title tag{extra}, so nothing in the files says which book this is.")
        if matches and mismatches:
            return "audiobook_tags", (
                f"{matches} of {tracks or matches + mismatches} tracks are tagged with “{title}” and "
                f"{mismatches} with something else"
                + (f" (mostly {tagged})" if tagged else "") + ", so the tags do not agree on one book.")
        return "audiobook_tags", (
            (f"The tracks' tags say {tagged}; " if tagged else "")
            + f"Bindery has “{title}”{_by(who['expectedAuthor'])}. The tags do not clearly name this book"
            + (f": {evidence.get('explanation')}" if evidence.get("explanation") else "."))
    if not who["hasContentSection"]:
        # Nothing was read: the file could not be opened, copied or was too large to check.
        return "not_checked", ("BookGuard could not check this file: "
                               + (str(evidence.get("explanation") or "it could not be read") + ""))
    placeholder = normalize(who["fileAuthor"]) in PLACEHOLDER_AUTHORS
    if who["textCharacters"] == 0:
        notes = " ".join(who["notes"])
        if "DRM" in notes:
            reason = "This Kindle book is DRM-protected, so its text cannot be read."
            kind = "drm"
        elif suffix and suffix not in _READABLE_SUFFIXES:
            reason = f"BookGuard cannot read {suffix} files, so their content could not be compared with the book."
            kind = "unsupported_format"
        elif "extraction error" in notes:
            reason = "BookGuard could not read this file: " + notes.split("Native extraction error:")[-1].strip()
            kind = "extract_error"
        else:
            reason = ("The file opened but contains no readable text, so its content could not be "
                      "compared with the book. It may be image-only or empty.")
            kind = "no_text"
            ocr = who["ocr"]
            if ocr and ocr.get("error"):
                reason += f" BookGuard tried to read the page images with OCR: {ocr['error']}"
            elif ocr and ocr.get("attempted"):
                reason += " OCR of the first pages found no text either."
        if not who["fileTitle"] and not who["fileAuthor"]:
            reason += " The file carries no title or author of its own either."
        elif placeholder:
            reason += (f" Its own details (“{who['fileTitle'] or 'untitled'}” by “{who['fileAuthor']}”) "
                       "look like placeholders, so they do not identify it either.")
        else:
            reason += (f" The file calls itself “{who['fileTitle'] or 'untitled'}”{_by(who['fileAuthor'])}; "
                       f"Bindery has it as “{who['expectedTitle']}”{_by(who['expectedAuthor'])}.")
        return kind, reason
    ocr = who["ocr"]
    if ocr and ocr.get("chars"):
        return _why_scan(evidence, who, ocr, placeholder)
    parts = []
    kind = "other"
    if who["fileTitle"] or who["fileAuthor"]:
        parts.append(f"The file calls itself “{who['fileTitle'] or 'untitled'}”{_by(who['fileAuthor'])}; "
                     f"Bindery has it as “{who['expectedTitle']}”{_by(who['expectedAuthor'])}.")
    else:
        parts.append("The file carries no title or author of its own.")
        kind = "no_metadata"
    title_in, author_in = who["expectedTitleInText"], who["expectedAuthorInText"]
    if title_in and author_in:
        kind = "both_found"
        parts.append("Both the title and the author appear in the book, but not in a place that proves "
                     "they identify it rather than a series list or advertisement.")
    elif title_in:
        kind = "author_missing"
        parts.append(f"The title appears in the book but the author “{who['expectedAuthor']}” does not; "
                     "Bindery’s author name may be spelled differently from the book’s.")
    elif author_in:
        kind = "title_missing"
        parts.append(f"The author appears in the book but the title “{who['expectedTitle']}” does not; "
                     "it may be a different title, edition or translation.")
    elif who["fileTitleInText"] or who["fileAuthorInText"]:
        kind = "other_book"
        parts.append("Neither Bindery’s title nor its author appears in the book, but the file’s own title "
                     "does, so this may be a different book.")
    else:
        kind = "neither_found" if kind == "other" else kind
        parts.append("Neither the title nor the author could be found in the readable text.")
    for step in evidence.get("secondPass") or []:
        if step.get("check") == "catalogueSearch":
            count = int(step.get("matches") or 0)
            parts.append("BookGuard also searched your Bindery library for a book matching the file’s own "
                         + ("title and author and found none." if not count
                            else f"title and author and found {count}, too many to pick one safely."))
    return kind, " ".join(parts)

def _why_scan(evidence: dict, who: dict, ocr: dict, placeholder: bool) -> tuple[str, str]:
    """Why a scan (a PDF whose text is OCR of its first pages) is undecided."""
    pages, aside = int(ocr.get("pages") or 0), int(ocr.get("setAside") or 0)
    read = f"BookGuard read the first {pages} page{'s' if pages != 1 else ''} of this scan with OCR"
    if aside:
        read += f", setting aside {aside} page{'s' if aside != 1 else ''} that list other books"
    title, by = who["expectedTitle"], _by(who["expectedAuthor"])
    if evidence.get("scanPage"):
        if not (who["fileTitle"] or who["fileAuthor"]):
            details = "carries no title or author"
        elif placeholder:
            details = f"look like placeholders (“{who['fileTitle'] or 'untitled'}” by “{who['fileAuthor']}”)"
        else:
            details = f"say “{who['fileTitle'] or 'untitled'}”{_by(who['fileAuthor'])}, not this book"
        history = next((step for step in evidence.get("secondPass") or [] if step.get("check") == "scanProvenance"), None)
        if history is None or not int(history.get("releases") or 0):
            record = "Bindery has no download record for this book"
        else:
            record = "Bindery's download record does not name this exact title and author"
        return "scan_page_only", (
            f"{read}. The first page reads as “{title}”{by}, which is this book, but a scan's first page is "
            f"only one source (a cover can name other books) and nothing else confirms it: the file's own "
            f"details {details}, and {record}. An ISBN, or a download record, would settle it.")
    signal = (evidence.get("content") or {}).get("expected_signal") or {}
    reason = f"{read}, but it does not open with “{title}”{by}: a scan is matched only by a first page that shows the title and the author together."
    if signal.get("title_near") and not signal.get("title_found"):
        reason += " A passage close to the title does appear, which may be an OCR misreading of it."
    elif signal.get("title_found") and not signal.get("author_found"):
        reason += f" The title is there, but the author “{who['expectedAuthor']}” is not credited beside it."
    elif signal.get("author_found") and not signal.get("title_found"):
        reason += " The author is there, but the title is not what the first page opens with."
    return "scan_not_opening", reason


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
    suffix = Path(str(row.get("stored_path") or "")).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        suffix = ""  # a dotted folder name ("Mr. Mercedes") is not a file type
    why = (_why_undecided(evidence, suffix, str(row.get("format") or "ebook"))
           if verification and group == "undecided" else ("", ""))
    mixed_audio = ("MIXED_AUDIO_CONTENT" in {row.get("reason_code"), evidence.get("reasonCode")}
                   or why[0] == "audiobook_collection")
    return {
        "id": int(row["id"]),
        "fileType": suffix,
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
        "whyKind": why[0],
        "whyUndecided": why[1],
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


UNDECIDED_KINDS = {
    "drm": "DRM-protected", "unsupported_format": "Unsupported file type", "extract_error": "Could not be read",
    "no_text": "No readable text", "no_metadata": "No title or author inside the file",
    "author_missing": "Author not found in the book", "title_missing": "Title not found in the book",
    "scan_page_only": "Scan: first page agrees, nothing else confirms it",
    "scan_not_opening": "Scan: first page does not show this book",
    "both_found": "Title and author found, position unclear", "other_book": "May be a different book",
    "neither_found": "Neither found in the text", "audiobook_collection": "Collection of several books",
    "audiobook_tags": "Audio tags do not name the book", "audiobook_other_work": "Audio tags name a different book",
    "audiobook_no_tags": "Audiobook has no usable tags", "not_checked": "File could not be read for checking", "volume_conflict": "Volume or part conflict",
    "other": "Other",
}


def undecided_breakdown(items: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Why the undecided books are undecided, most common first: (label, count)."""
    counts: dict[str, int] = {}
    for item in items:
        if item["group"] != "undecided":
            continue
        if item.get("whyKind"):
            label = UNDECIDED_KINDS.get(item["whyKind"], "Other")
        else:  # nothing recorded yet: it was never verified, or the virus scan gave no result
            label = "Virus scan gave no result" if item.get("scannerOutage") else "Not verified yet"
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))

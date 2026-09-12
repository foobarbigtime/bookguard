from __future__ import annotations

import re

from .matcher import author_match_strict, author_mentioned_in_text, meaningful_words, normalize
from .verification_constants import FRONT_TEXT_CHARS

PROXIMITY_CHARS = 3_000
METADATA_ERROR_MAX_PROXIMITY = 500
METADATA_ERROR_MAX_TITLE_POSITION = 20_000
CONFLICTING_TITLE_PRIMARY_POSITION = 500

COLLECTION_TITLE_PATTERNS = (
    r"\bcomplete(?:\s+[\w'’-]+){0,4}\s+(?:series|collection|works|novels|stories)\b",
    r"\bomnibus\b",
    r"\bbox(?:ed)?\s+set\b",
    r"\bcollection\b",
    r"\banthology\b",
)


def _phrase_pos(value: str, text: str) -> int:
    needle = normalize(value)
    haystack = normalize(text)
    if not needle or not haystack:
        return -1
    return f" {haystack} ".find(f" {needle} ")


def _author_positions(author: str, text: str) -> list[int]:
    haystack = normalize(text)
    if not haystack:
        return []
    candidates = [part.strip() for part in re.split(r"[;|]", author or "") if part.strip()]
    if not candidates and author:
        candidates = [author]
    positions: list[int] = []
    for candidate in candidates:
        normalized = normalize(candidate)
        if normalized:
            pos = f" {haystack} ".find(f" {normalized} ")
            if pos >= 0:
                positions.append(pos)
        # Also accept the existing tolerant author matcher as presence evidence,
        # but not as proximity evidence when no exact normalized name is found.
    return positions


def _author_found(author: str, text: str) -> bool:
    candidates = [part.strip() for part in re.split(r"[;|]", author or "") if part.strip()]
    if not candidates and author:
        candidates = [author]
    return any(author_mentioned_in_text(candidate, text) for candidate in candidates)


def _identity_signal(title: str, author: str, full_text: str, front_text: str) -> dict:
    title_full = _phrase_pos(title, full_text)
    title_front = _phrase_pos(title, front_text)
    author_full_positions = _author_positions(author, full_text)
    author_front_positions = _author_positions(author, front_text)
    author_any = _author_found(author, full_text)
    author_front_any = _author_found(author, front_text)

    proximity = False
    proximity_distance = None
    if title_front >= 0 and author_front_positions:
        distance = min(abs(title_front - pos) for pos in author_front_positions)
        proximity_distance = distance
        proximity = distance <= PROXIMITY_CHARS

    title_found = title_full >= 0
    strong = title_front >= 0 and author_front_any and (proximity or title_front < 40_000)
    return {
        "title_found": title_found,
        "author_found": author_any,
        "title_front_found": title_front >= 0,
        "author_front_found": author_front_any,
        "front_proximity": proximity,
        "front_proximity_chars": proximity_distance,
        "strong_identity": strong,
        "title_first_position": title_full if title_full >= 0 else None,
        "author_exact_first_position": min(author_full_positions) if author_full_positions else None,
    }


def _title_identity_match(expected: str, observed: str) -> bool:
    e = normalize(expected)
    o = normalize(observed)
    if not e or not o:
        return False
    if e == o:
        return True
    ew = meaningful_words(expected)
    ow = meaningful_words(observed)
    return bool(ew) and ew == ow


def _classify_identity_base(
    result: dict,
    metadata: dict,
    text: str,
    identifiers: list[str],
    notes: list[str],
    front_text: str = "",
) -> tuple[str, int, dict]:
    expected_title = str(result.get("title") or "")
    expected_author = str(result.get("author") or "")
    embedded_title = str(metadata.get("title") or "")
    embedded_author = str(metadata.get("author") or "")
    front_text = front_text or text[:FRONT_TEXT_CHARS]

    expected = _identity_signal(expected_title, expected_author, text, front_text)
    embedded = _identity_signal(embedded_title, embedded_author, text, front_text)

    metadata_title_match = _title_identity_match(expected_title, embedded_title)
    metadata_author_match = author_match_strict(expected_author, embedded_author)
    metadata_matches_expected = metadata_title_match and metadata_author_match

    evidence = {
        "expected": {"title": expected_title, "author": expected_author},
        "embedded": {"title": embedded_title, "author": embedded_author, "identifiers": identifiers},
        "content": {
            # Preserve v0.4.7 keys for the UI/export while adding stronger evidence.
            "expected_title_found": expected["title_found"],
            "expected_author_found": expected["author_found"],
            "embedded_title_found": embedded["title_found"],
            "embedded_author_found": embedded["author_found"],
            "text_characters_examined": len(text),
            "front_text_characters_examined": len(front_text),
            "expected_signal": expected,
            "embedded_signal": embedded,
        },
        "metadata_matches_expected": metadata_matches_expected,
        "notes": notes,
    }

    if metadata_matches_expected and expected["strong_identity"]:
        evidence["explanation"] = (
            "Embedded metadata matches the expected book and front-of-book content "
            "independently supports that identity."
        )
        return "VERIFIED_CORRECT", 99, evidence

    if not metadata_matches_expected and expected["strong_identity"] and not embedded["strong_identity"]:
        evidence["explanation"] = (
            "Front-of-book content strongly identifies the expected Bindery book, while "
            "the conflicting embedded identity is not strongly supported there."
        )
        return "METADATA_ERROR", 97, evidence

    if (
        not metadata_matches_expected
        and embedded_title
        and embedded_author
        and embedded["strong_identity"]
        and not expected["title_found"]
        and not expected["author_found"]
    ):
        evidence["explanation"] = (
            "Front-of-book content strongly supports the conflicting embedded title and "
            "author, while neither expected identity field was found anywhere in extracted content."
        )
        return "WRONG_CONTENT", 99, evidence

    if metadata_matches_expected and (expected["title_found"] or expected["author_found"]):
        evidence["explanation"] = (
            "Metadata matches the expected book, but content evidence is not positioned "
            "strongly enough for a 99% verdict."
        )
        return "VERIFIED_CORRECT", 90, evidence

    if expected["title_found"] and expected["author_found"] and not metadata_matches_expected:
        evidence["explanation"] = (
            "Expected title and author occur in the book, but their location/proximity is not "
            "strong enough to distinguish true identity from backmatter, series lists, or advertisements."
        )
        return "INSUFFICIENT_EVIDENCE", 70, evidence

    evidence["explanation"] = (
        "BookGuard could not obtain position-aware identity evidence strong enough "
        "for an automatic verdict."
    )
    return "INSUFFICIENT_EVIDENCE", 40, evidence


def _downgrade_metadata_error(evidence: dict, reason: str) -> tuple[str, int, dict]:
    evidence["explanation"] = reason
    evidence.setdefault("notes", []).append(
        "Automatic metadata repair withheld by the structural safety gate."
    )
    return "INSUFFICIENT_EVIDENCE", 70, evidence


def _title_is_strict_subphrase(shorter: str, longer: str) -> bool:
    short = normalize(shorter)
    long = normalize(longer)
    return bool(short and long and short != long and f" {short} " in f" {long} ")


def _looks_collection_like(title: str) -> bool:
    value = str(title or "").strip().lower()
    return bool(value) and any(
        re.search(pattern, value, flags=re.I)
        for pattern in COLLECTION_TITLE_PATTERNS
    )


def classify_identity(
    result: dict,
    metadata: dict,
    text: str,
    identifiers: list[str],
    notes: list[str],
    front_text: str = "",
) -> tuple[str, int, dict]:
    """Classify identity, then apply every automatic-repair safety refinement."""
    verdict, confidence, evidence = _classify_identity_base(
        result, metadata, text, identifiers, notes, front_text
    )
    if verdict != "METADATA_ERROR":
        return verdict, confidence, evidence

    expected_signal = evidence.get("content", {}).get("expected_signal", {})
    embedded_signal = evidence.get("content", {}).get("embedded_signal", {})
    proximity = expected_signal.get("front_proximity_chars")
    title_position = expected_signal.get("title_first_position")
    embedded_title_position = embedded_signal.get("title_first_position")

    expected_title = str(evidence.get("expected", {}).get("title") or "")
    expected_author = str(evidence.get("expected", {}).get("author") or "")
    embedded_title = str(evidence.get("embedded", {}).get("title") or "")
    embedded_author = str(evidence.get("embedded", {}).get("author") or "")

    metadata_title_match = _title_identity_match(expected_title, embedded_title)
    metadata_author_match = author_match_strict(expected_author, embedded_author)

    if _looks_collection_like(embedded_title):
        return _downgrade_metadata_error(
            evidence,
            "The embedded title looks like a collection, omnibus, anthology, or boxed set; "
            "the expected title may be only one component, so automatic metadata repair is withheld.",
        )
    if not metadata_title_match and _title_is_strict_subphrase(expected_title, embedded_title):
        return _downgrade_metadata_error(
            evidence,
            "The expected title is only a shorter phrase within the conflicting embedded title, "
            "so it is not independent evidence for an automatic metadata rewrite.",
        )
    if metadata_title_match and not metadata_author_match:
        return _downgrade_metadata_error(
            evidence,
            "The embedded title matches the expected title but the author conflicts; "
            "author-only disagreement is kept for review rather than rewritten automatically.",
        )
    if (
        not expected_signal.get("front_proximity")
        or proximity is None
        or proximity > METADATA_ERROR_MAX_PROXIMITY
    ):
        return _downgrade_metadata_error(
            evidence,
            "The expected title and author occur near the front, but not tightly enough together "
            "for a safe automatic metadata rewrite.",
        )
    if title_position is None or title_position > METADATA_ERROR_MAX_TITLE_POSITION:
        return _downgrade_metadata_error(
            evidence,
            "The expected identity appears too far into the book to distinguish it safely from "
            "advertisements, series lists, or backmatter.",
        )
    if (
        embedded_title_position is not None
        and embedded_title_position <= CONFLICTING_TITLE_PRIMARY_POSITION
        and not metadata_title_match
    ):
        return _downgrade_metadata_error(
            evidence,
            "A conflicting embedded title appears at the very start of the book, so the expected "
            "identity is not sufficiently dominant for automatic repair.",
        )

    evidence["explanation"] = (
        "Title-page-like frontmatter tightly pairs the expected title and author, "
        "while no competing embedded identity has equally strong structural support."
    )
    return "METADATA_ERROR", 97, evidence

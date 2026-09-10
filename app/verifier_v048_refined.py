from __future__ import annotations

from . import verifier as legacy
from . import verifier_v048 as base
from .matcher import author_match_strict, normalize

# This refinement deliberately invalidates the previous v0.4.8 cache generation.
# It keeps the same public BookGuard version while forcing fresh verifier results.
legacy.VERIFIER_VERSION = "4"

# Automatic metadata repair should require title-page-like evidence, not merely
# two strings occurring somewhere in the first 160k characters.
METADATA_ERROR_MAX_PROXIMITY = 500
METADATA_ERROR_MAX_TITLE_POSITION = 20_000
CONFLICTING_TITLE_PRIMARY_POSITION = 500

_base_classify_identity = base._classify_identity


def _downgrade_metadata_error(evidence: dict, reason: str) -> tuple[str, int, dict]:
    evidence["explanation"] = reason
    evidence.setdefault("notes", []).append(
        "Automatic metadata repair withheld by the stricter v0.4.8 structural safety gate."
    )
    return "INSUFFICIENT_EVIDENCE", 70, evidence


def _title_is_strict_subphrase(shorter: str, longer: str) -> bool:
    """True when one normalized title is a complete word-sequence inside a longer title."""
    short = normalize(shorter)
    long = normalize(longer)
    if not short or not long or short == long:
        return False
    return f" {short} " in f" {long} "


def _classify_identity(
    result: dict,
    metadata: dict,
    text: str,
    identifiers: list[str],
    source: str,
    notes: list[str],
    front_text: str = "",
) -> tuple[str, int, dict]:
    verdict, confidence, evidence = _base_classify_identity(
        result, metadata, text, identifiers, source, notes, front_text
    )

    # WRONG_CONTENT and VERIFIED_CORRECT retain the already-conservative v0.4.8
    # logic. Only METADATA_ERROR receives a stricter repair-safety gate.
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

    metadata_title_match = base._title_identity_match(expected_title, embedded_title)
    metadata_author_match = author_match_strict(expected_author, embedded_author)

    # If the expected title is merely a shorter phrase inside the conflicting
    # embedded title (e.g. "Return" inside "The Return (BookShots Flames)"),
    # finding that short phrase in the content is not independent identity
    # evidence. Never use that pattern to authorize an automatic rewrite.
    if (
        not metadata_title_match
        and _title_is_strict_subphrase(expected_title, embedded_title)
    ):
        return _downgrade_metadata_error(
            evidence,
            "The expected title is only a shorter phrase within the conflicting embedded title, so it is not independent evidence for an automatic metadata rewrite.",
        )

    # Same-title/different-author records are too ambiguous for an automatic
    # metadata rewrite. They can represent anthologies, editions, contributors,
    # publishers, or incorrectly assigned Bindery records.
    if metadata_title_match and not metadata_author_match:
        return _downgrade_metadata_error(
            evidence,
            "The embedded title matches the expected title but the author conflicts; author-only disagreement is kept for review rather than rewritten automatically.",
        )

    # Require an exact title/author pairing that looks like title-page evidence.
    if not expected_signal.get("front_proximity") or proximity is None or proximity > METADATA_ERROR_MAX_PROXIMITY:
        return _downgrade_metadata_error(
            evidence,
            "The expected title and author occur near the front, but not tightly enough together for a safe automatic metadata rewrite.",
        )

    if title_position is None or title_position > METADATA_ERROR_MAX_TITLE_POSITION:
        return _downgrade_metadata_error(
            evidence,
            "The expected identity appears too far into the book to distinguish it safely from advertisements, series lists, or backmatter.",
        )

    # If a conflicting embedded title itself occupies the very start of the
    # book, do not overwrite it solely because another title/author pair also
    # appears soon afterward. That pattern is common in collections and bad
    # Bindery associations.
    if (
        embedded_title_position is not None
        and embedded_title_position <= CONFLICTING_TITLE_PRIMARY_POSITION
        and not metadata_title_match
    ):
        return _downgrade_metadata_error(
            evidence,
            "A conflicting embedded title appears at the very start of the book, so the expected identity is not sufficiently dominant for automatic repair.",
        )

    evidence["explanation"] = (
        "Title-page-like frontmatter tightly pairs the expected title and author, "
        "while no competing embedded identity has equally strong structural support."
    )
    return "METADATA_ERROR", 97, evidence


# Patch the v0.4.8 module so its mature extraction and verification path uses
# the stricter classifier, then patch the legacy job/repair machinery as well.
base._classify_identity = _classify_identity
legacy.verify_result = base.verify_result

verify_result = base.verify_result
init_verification_db = base.init_verification_db
verification_for_result = base.verification_for_result
verification_summary = base.verification_summary
verification_job_status = base.verification_job_status
start_verification_job = base.start_verification_job
test_tika = base.test_tika
verified_repair_preview = base.verified_repair_preview
apply_verified_metadata_repair = base.apply_verified_metadata_repair

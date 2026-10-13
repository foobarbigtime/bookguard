from __future__ import annotations

from difflib import SequenceMatcher
import re

import sqlite3

from .identity_search import second_pass
from .matcher import author_match_strict, author_mentioned_in_text, author_names, meaningful_words, normalize
from .scan_text import PAGE_BREAK, without_book_lists
from .series_titles import ebook_title_conflict, expected_title_variants
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

GENERIC_TITLE_SUFFIX = re.compile(
    r"\s*[:;,\-\u2013\u2014]\s*(?:a\s+)?novel\s*$",
    flags=re.IGNORECASE,
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


# --- OCR text ------------------------------------------------------------------------------
# A scan's text comes from OCR, which drops spaces and misreads letters, and the page is the only
# witness to what the book is. So scanned text is held to a stricter rule than a text layer:
#   opening - a scan is the book whose title it OPENS with: the title must be on the first page
#           that has text, among its first lines (a cover, a half-title or a title page). A
#           promotion, a contents page, a series header, a dedication or a running header further
#           in prints other titles beside the author's name just as a title page does, so nothing
#           after the first page can establish what the book is.
#   own line - the TITLE fills its own line(s) (a leading "The", and a tail that is "A Novel" or
#           "by <the author>", are allowed) and the AUTHOR stands on a line of their own, optionally
#           after "by" / "a novel by". "Dune" is not "Dune Messiah", "It" is not a word in a
#           paragraph, "Love" is not "Love Story", and a foreword by the author is not the author.
#   exact - spaces and punctuation are ignored, nothing else. Only an exact match is evidence FOR a
#           book; title and author must also be on one page, close together, to be "strong".
#   near  - a very close but not identical passage (a misread letter). It is never evidence for
#           a book ("The Lost Colony" is one letter from "The Last Colony"); it is used only to
#           hold back a verdict that would reject or rewrite a book that OCR may have misread.
# Pages that list books ("Also by ...") are set aside first: see scan_text.
OCR_MIN_NEAR = 10            # shorter names tolerate no misread letter at 90%, so they are exact-only
OCR_MAX_NEAR_NEEDLE = 80     # longer names are matched exactly only (a PDF /Title can be anything)
OCR_NEAR_RATIO = 0.9
OCR_MAX_CHARS = 30_000       # OCR text is capped at 20,000 characters upstream; never scan more
OCR_GRAM = 4
OCR_MAX_GRAM_HITS = 200      # a fragment this common says nothing about where a title is
OCR_MAX_CANDIDATES = 16      # at most this many places are compared closely, whatever the text
OCR_CANDIDATE_SLACK = 2
OCR_MAX_EXACT_HITS = 2_000
OCR_TITLE_PAGE_CHARS = 400   # a title page prints the title and the author within a few lines
OCR_OPENING_LINES = 8        # the title is among the first lines of the first page
OCR_MIN_AUTHOR = 5           # as the text-layer matcher: shorter names are never matched
_OCR_LEADING = {"", "the"}   # "A" / "An" are part of a different title ("A Dog's Purpose")
_OCR_DESCRIPTOR = r"(?:(?:a|an|the)\s+)?(?:novel|memoir|thriller)"
_OCR_TITLE_BY = re.compile(rf"(?:{_OCR_DESCRIPTOR}\s+)?by\s+(.+)")             # "A Novel by John Scalzi"
_OCR_DESCRIPTOR_ONLY = re.compile(_OCR_DESCRIPTOR)                              # ": A Novel"
_OCR_AUTHOR_LEAD = re.compile(rf"(?:{_OCR_DESCRIPTOR}(?:\s+|$))?(?:written(?:\s+|$))?(?:by)?")
_OCR_AUTHOR_TAIL = re.compile(r"and\s+\S.*")                                   # a co-author may follow
_OCR_GENERIC_TITLES = frozenset({
    "novel", "anovel", "thenovel", "memoir", "amemoir", "thriller", "athriller", "mystery", "amystery",
    "story", "astory", "stories", "book", "abook", "tale", "atale", "romance", "aromance",
})


class _Squashed:
    """One OCR page reduced to letters and digits, with each character's place in the readable text.

    ``readable`` keeps the page's lines (words lower-cased and separated by spaces, lines by
    newlines) so a match can be checked against the line it sits on."""

    __slots__ = ("text", "origin", "readable")

    def __init__(self, value: str):
        lines = [normalize(line) for line in (value or "").split("\n")]
        self.readable = "\n".join(lines)[:OCR_MAX_CHARS * 2]
        kept = [(char, index) for index, char in enumerate(self.readable) if char not in " \n"]
        self.text = "".join(char for char, _ in kept)[:OCR_MAX_CHARS]
        self.origin = [index for _, index in kept][:OCR_MAX_CHARS]

    def opening_end(self, lines: int = OCR_OPENING_LINES) -> int:
        """Where the first ``lines`` non-empty lines of the page end, in the readable text."""
        seen, index = 0, 0
        for line in self.readable.split("\n"):
            index += len(line) + 1
            if line.strip():
                seen += 1
                if seen >= lines:
                    break
        return index


def _squash(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(value))


def _title_tail_ok(tail: str, authors: frozenset[str]) -> bool:
    if not tail or _OCR_DESCRIPTOR_ONLY.fullmatch(tail):
        return True
    by = _OCR_TITLE_BY.fullmatch(tail)
    return (_squash(by.group(1)) in authors) if by else (_squash(tail) in authors)


def _ocr_exact_pos(
    needle: str, squashed: _Squashed, *, title: bool = False, authors: frozenset[str] = frozenset(),
    after_title: str = "",
) -> int:
    """Position (in the page's readable text) of ``needle`` standing alone on its line(s), else -1.

    Whole words: nothing alphanumeric touches either end. A TITLE (``title=True``) fills its
    lines, bar a leading "The" and a tail that is a descriptor (": A Novel") or "by <author>" -
    ``authors`` are the squashed names that count as the author. A name (the default) stands on
    a line of its own, after at most "by" / "a novel by", and is followed by at most a co-author."""
    if not needle or needle in _OCR_GENERIC_TITLES:
        return -1
    readable = squashed.readable
    start = 0
    for _ in range(OCR_MAX_EXACT_HITS):
        hit = squashed.text.find(needle, start)
        if hit < 0:
            return -1
        first, last = squashed.origin[hit], squashed.origin[hit + len(needle) - 1]
        before = readable[first - 1] if first > 0 else " "
        after = readable[last + 1] if last + 1 < len(readable) else " "
        if not before.isalnum() and not after.isalnum():
            line_start = readable.rfind("\n", 0, first) + 1
            line_end = readable.find("\n", last + 1)
            lead = readable[line_start:first].strip()
            tail = readable[last + 1:line_end if line_end >= 0 else len(readable)].strip()
            if title:
                if lead in _OCR_LEADING and _title_tail_ok(tail, authors):
                    return first
            elif (_OCR_AUTHOR_LEAD.fullmatch(lead)
                  or (after_title and _squash(re.sub(r"\s*\bby$", "", lead)) == after_title)) and (
                    not tail or _OCR_AUTHOR_TAIL.fullmatch(tail)):
                return first
        start = hit + 1
    return -1


def _ocr_near_pos(needle: str, squashed: _Squashed, min_length: int) -> int:
    """Position of a passage within a letter or two of ``needle``, else -1. Never evidence FOR a book.

    Work is bounded whatever the text or the name: the text is indexed once by short fragments,
    the few places where many fragments line up are compared closely (at most
    ``OCR_MAX_CANDIDATES`` places), and every number in the name must appear intact
    ("Treehouse 13" is not "26")."""
    size = len(needle)
    if size < min_length or size > OCR_MAX_NEAR_NEEDLE:
        return -1
    haystack = squashed.text
    grams = [needle[i:i + OCR_GRAM] for i in range(size - OCR_GRAM + 1)]
    wanted = set(grams)
    index: dict[str, list[int]] = {}
    for i in range(len(haystack) - OCR_GRAM + 1):
        gram = haystack[i:i + OCR_GRAM]
        if gram in wanted:
            index.setdefault(gram, []).append(i)
    votes: dict[int, int] = {}
    for offset, gram in enumerate(grams):
        hits = index.get(gram, ())
        if len(hits) > OCR_MAX_GRAM_HITS:
            continue
        for place in hits:
            votes[place - offset] = votes.get(place - offset, 0) + 1
    needed = max(2, int(len(grams) * 0.4))
    ranked = sorted((item for item in votes.items() if item[1] >= needed), key=lambda item: -item[1])
    numbers = re.findall(r"\d+", needle)
    best, best_at, tried = 0.0, -1, set()
    for diagonal, _ in ranked[:OCR_MAX_CANDIDATES]:
        for start in range(diagonal - OCR_CANDIDATE_SLACK, diagonal + OCR_CANDIDATE_SLACK + 1):
            window = haystack[start:start + size] if start >= 0 else ""
            # Extra digits are fine, since OCR reads a letter as a digit ("SW0RD").
            if start in tried or len(window) < size or not set(numbers) <= set(re.findall(r"\d+", window)):
                continue
            tried.add(start)
            matcher = SequenceMatcher(None, needle, window, autojunk=False)
            if matcher.quick_ratio() >= OCR_NEAR_RATIO and (ratio := matcher.ratio()) > best:
                best, best_at = ratio, start
    return squashed.origin[best_at] if best >= OCR_NEAR_RATIO and best_at >= 0 else -1


def _ocr_identity_signal(title: str, author: str, full_text: str, front_text: str) -> dict:
    """Like _identity_signal, for OCR text (see the notes above).

    ``title_found`` means the book OPENS with this title; ``author_found`` that the author is credited
    on a line of their own. They, and everything built on them (strong identity, the "verified"
    verdicts, the second pass), are exact matches only. ``title_near`` / ``author_near`` /
    ``near_identity`` (any page, any close passage) exist so a book OCR may have misread is not
    rejected or rewritten."""
    names = [part for credit in (author or "").split(";") for part in author_names(credit.strip()) if part.strip()]
    squashed_names = frozenset(name for name in (_squash(part) for part in names) if len(name) >= OCR_MIN_AUTHOR)
    squashed_title = _squash(title or "")
    # OCR text is far shorter than the front-text window, so the two are the same text. Pages that
    # list other books are already gone when it comes from ocr_probe; doing it again costs nothing
    # and means no caller can hand this function a list page.
    pages = [_Squashed(page) for page in without_book_lists(full_text)[0].split(PAGE_BREAK) if page.strip()][:12]

    def find_author(page: _Squashed) -> int:
        found = [pos for name in squashed_names
                 if (pos := _ocr_exact_pos(name, page, authors=squashed_names, after_title=squashed_title)) >= 0]
        return min(found) if found else -1

    def near_author_in(page: _Squashed) -> bool:
        return any(_ocr_near_pos(name, page, OCR_MIN_NEAR) >= 0 for name in squashed_names)

    opening_at = None                      # where the first page's opening lines show the title
    author_at = None                       # first credit of the author, over all pages
    tight: int | None = None               # closest title/author pair printed on one page
    near_title = near_author = False
    offset = 0
    for number, page in enumerate(pages):
        t = _ocr_exact_pos(squashed_title, page, title=True, authors=squashed_names)
        a = find_author(page)
        if number == 0 and 0 <= t < page.opening_end():
            opening_at = t
        if a >= 0 and author_at is None:
            author_at = offset + a
        if t >= 0 and a >= 0 and abs(t - a) <= OCR_TITLE_PAGE_CHARS:
            tight = abs(t - a) if tight is None else min(tight, abs(t - a))
        near_title = near_title or t >= 0 or _ocr_near_pos(squashed_title, page, OCR_MIN_NEAR) >= 0
        near_author = near_author or a >= 0 or near_author_in(page)
        offset += len(page.readable) + 1
    found_title, found_author = opening_at is not None, author_at is not None
    strong = found_title and tight is not None
    return {
        "title_found": found_title, "author_found": found_author,
        "title_front_found": found_title, "author_front_found": found_author,
        "front_proximity": strong, "front_proximity_chars": tight if strong else None,
        "strong_identity": strong,
        "title_first_position": opening_at,
        "author_exact_first_position": author_at,
        "ocr": True,
        "title_near": near_title, "author_near": near_author,
        "near_identity": bool(near_title and near_author),
    }


def _identity_signal(title: str, author: str, full_text: str, front_text: str, ocr: bool = False) -> dict:
    if ocr:
        return _ocr_identity_signal(title, author, full_text, front_text)
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

    # Catalogues commonly omit a leading article while publisher metadata adds
    # a non-distinguishing work-type suffix. Keep this deliberately narrow: a
    # separator is required before "A Novel", and no substantive subtitle is
    # discarded. Exact author and front-of-book evidence are still independently
    # required by the classifier before automatic admission can be authorized.
    def catalog_title(value: str) -> str:
        value = GENERIC_TITLE_SUFFIX.sub("", value or "")
        words = normalize(value).split()
        if words and words[0] in {"a", "an", "the"}:
            words = words[1:]
        return " ".join(words)

    catalog_expected = catalog_title(expected)
    catalog_observed = catalog_title(observed)
    if catalog_expected and catalog_expected == catalog_observed:
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
    series_names = [str(name) for name in (result.get("series") or []) if str(name or "").strip()]

    # A catalogue title may carry its series ("Mary, Mary: Alex Cross, Book 11")
    # while the book says only "Mary, Mary". The series-free title is an extra
    # candidate; the full title is tried first and wins ties. Author, metadata,
    # and front-of-book requirements below are unchanged.
    title_variants = expected_title_variants(expected_title, series_names) or [expected_title]
    ocr_text = bool(result.get("_ocr_text"))
    matched_title, expected = max(
        (
            (variant, _identity_signal(variant, expected_author, text, front_text, ocr_text))
            for variant in title_variants
        ),
        key=lambda item: (
            item[1]["strong_identity"],
            item[1]["front_proximity"],
            item[1]["title_front_found"],
            item[1]["title_found"],
        ),
    )
    embedded = _identity_signal(embedded_title, embedded_author, text, front_text, ocr_text)

    # Files carry series tags too ("The Lost Continent (Wings of Fire, Book 11)").
    # The same unambiguous rule applies to the file's own title.
    embedded_variants = expected_title_variants(embedded_title, series_names) or [embedded_title]
    metadata_title_match = any(
        _title_identity_match(variant, embedded_variant)
        for variant in title_variants
        for embedded_variant in embedded_variants
    )
    metadata_author_match = author_match_strict(expected_author, embedded_author)
    metadata_matches_expected = metadata_title_match and metadata_author_match

    expected_identity = {"title": expected_title, "author": expected_author}
    if len(title_variants) > 1:
        expected_identity.update(
            {
                "matchedTitle": matched_title,
                "titleVariants": title_variants[1:],
                "series": series_names,
            }
        )
    embedded_identity = {"title": embedded_title, "author": embedded_author, "identifiers": identifiers}
    if len(embedded_variants) > 1:
        embedded_identity["titleVariants"] = embedded_variants[1:]
    evidence = {
        "expected": expected_identity,
        "embedded": embedded_identity,
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

    if ebook_title_conflict(expected_title, embedded_title):
        evidence["metadata_matches_expected"] = False
        evidence["reasonCode"] = "TITLE_DESIGNATION_CONFLICT"
        evidence["explanation"] = (
            "The original catalogue and embedded titles disagree on a volume number "
            "or on whether this is one part of a work. Series-free title evidence "
            "cannot resolve that distinction."
        )
        return "INSUFFICIENT_EVIDENCE", 60, evidence

    if metadata_matches_expected and expected["strong_identity"]:
        evidence["explanation"] = (
            "Embedded metadata matches the expected book and front-of-book content "
            "independently supports that identity."
        )
        return "VERIFIED_CORRECT", 99, evidence

    # The file's own identity may be on the page but misread by OCR: that is not "unsupported".
    if (
        not metadata_matches_expected
        and expected["strong_identity"]
        and not embedded["strong_identity"]
        and not embedded.get("near_identity")
    ):
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
        and not expected.get("title_near")
        and not expected.get("author_near")
    ):
        evidence["explanation"] = (
            "Front-of-book content strongly supports the conflicting embedded title and "
            "author, while neither expected identity field was found anywhere in extracted content."
        )
        return "WRONG_CONTENT", 99, evidence

    # In a scan the page is the only independent witness: the title alone could be a different
    # author's book of that name, the author alone a different book by that author.
    content_supports = (
        (expected["title_found"] and expected["author_found"]) if ocr_text
        else (expected["title_found"] or expected["author_found"])
    )
    if metadata_matches_expected and content_supports:
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


def _metadata_title_matches(evidence: dict, title: str) -> bool:
    """Does the file's own title (or a series-free variant) match ``title``?"""
    embedded = evidence.get("embedded", {})
    if ebook_title_conflict(title, str(embedded.get("title") or "")):
        return False
    candidates = [str(embedded.get("title") or ""), *embedded.get("titleVariants", [])]
    targets = expected_title_variants(title) or [title]
    return any(
        _title_identity_match(target, candidate)
        for target in targets
        for candidate in candidates
        if target and candidate
    )


def _apply_isbn_evidence(
    verdict: str, confidence: int, evidence: dict, result: dict
) -> tuple[str, int, dict]:
    """Use the file's ISBN only when a second, independent signal agrees.

    - The ISBN is one of the expected book's editions and the file's own title
      matches the expected book: the file is that book.
    - The ISBN belongs to a different Bindery book and the file's own title
      matches that book: the file is that other book.

    Only undecided results are upgraded. An ISBN that contradicts a
    WRONG_CONTENT verdict sends it back for review; an ISBN on its own never
    decides anything.
    """
    isbn = result.get("isbn_evidence") or {}
    if not isbn.get("isbns"):
        return verdict, confidence, evidence
    evidence["isbn"] = isbn
    expected_title = str(evidence.get("expected", {}).get("title") or "")
    title_agrees = _metadata_title_matches(evidence, expected_title)

    if isbn.get("expectedMatch"):
        if verdict == "INSUFFICIENT_EVIDENCE" and title_agrees:
            evidence["explanation"] = (
                "The file's ISBN is one of this book's editions in Bindery, and the "
                "file's own title agrees with the expected book."
            )
            return "VERIFIED_CORRECT", 99, evidence
        if verdict == "WRONG_CONTENT":
            evidence["explanation"] = (
                "The content points to a different book, but the file's ISBN is one of "
                "this book's editions in Bindery. The evidence conflicts, so it is left "
                "for review."
            )
            return "INSUFFICIENT_EVIDENCE", 70, evidence
        return verdict, confidence, evidence

    if verdict != "INSUFFICIENT_EVIDENCE" or title_agrees:
        return verdict, confidence, evidence
    for owner in isbn.get("otherOwners") or []:
        if _metadata_title_matches(evidence, str(owner.get("title") or "")):
            evidence["actualBook"] = owner
            evidence["explanation"] = (
                f"The file's ISBN and its own title both identify \"{owner.get('title')}\" "
                f"(Bindery book {owner.get('bookId')}), not the expected book."
            )
            return "WRONG_CONTENT", 99, evidence
    return verdict, confidence, evidence


def classify_identity(
    result: dict,
    metadata: dict,
    text: str,
    identifiers: list[str],
    notes: list[str],
    front_text: str = "",
) -> tuple[str, int, dict]:
    """Classify identity, apply safety refinements, then corroborated ISBN evidence."""
    verdict, confidence, evidence = _classify_identity_refined(
        result, metadata, text, identifiers, notes, front_text
    )
    verdict, confidence, evidence = _apply_isbn_evidence(verdict, confidence, evidence, result)
    return _apply_second_pass(verdict, confidence, evidence, result)


def _apply_second_pass(
    verdict: str, confidence: int, evidence: dict, result: dict
) -> tuple[str, int, dict]:
    """Look for more evidence before leaving a book undecided.

    A volume or part conflict is never settled by looser matching, so it is left as is.
    """
    if verdict != "INSUFFICIENT_EVIDENCE" or evidence.get("reasonCode") == "TITLE_DESIGNATION_CONFLICT":
        return verdict, confidence, evidence
    try:
        found = second_pass(result, evidence)
    except (sqlite3.Error, OSError, ValueError):
        return verdict, confidence, evidence
    return (found[0], found[1], evidence) if found else (verdict, confidence, evidence)


def _classify_identity_refined(
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

    expected_info = evidence.get("expected", {})
    # Refinements judge the title the evidence was actually based on.
    expected_title = str(expected_info.get("matchedTitle") or expected_info.get("title") or "")
    expected_author = str(expected_info.get("author") or "")
    embedded_title = str(evidence.get("embedded", {}).get("title") or "")
    embedded_author = str(evidence.get("embedded", {}).get("author") or "")

    embedded_info = evidence.get("embedded", {})
    metadata_title_match = any(
        _title_identity_match(candidate, embedded_candidate)
        for candidate in [str(expected_info.get("title") or ""), *expected_info.get("titleVariants", [])]
        for embedded_candidate in [embedded_title, *embedded_info.get("titleVariants", [])]
        if candidate and embedded_candidate
    )
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

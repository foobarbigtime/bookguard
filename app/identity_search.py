"""Second-pass identity evidence for books the first pass could not decide.

The first pass compares the file with the Bindery book it is assigned to and
needs an exact, well-positioned match. When that is not enough, this pass looks
for more evidence instead of giving up:

1. Tolerant identity: the file's own title and author are compared with the
   assigned book allowing for initials, "Surname, Given" order and
   extra credited authors. A tolerant match counts only when the book's text
   also contains the assigned title or author.
2. Catalogue search: the file's own title and author are searched for among the
   other books in Bindery. If exactly one other book matches and the book's text
   supports the file's own identity, the file is that other book.

Read-only: nothing is written to Bindery or to any file. Every check that ran
is recorded in the evidence, so the operator can see what was tried.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .db import bindery_conn
from .matcher import author_match_strict
from .provenance import provenance_proof, releases_for_book
from .series_titles import ebook_title_conflict, expected_title_variants
from .title_matching import edition_markers, meaningful_words, normalize

MAX_CANDIDATES = 5
# Bump when the second pass gets stricter, so earlier second-pass verdicts are re-examined.
SECOND_PASS_VERSION = 2
TOLERANT_CONFIDENCE = 88
ISBN_CONFIDENCE = 95
PROVENANCE_CONFIDENCE = 90
CATALOGUE_CONFIDENCE = 95

_PEOPLE = re.compile(r"\s*(?:;|&|\||\band\b|\bwith\b)\s*", re.IGNORECASE)
_GENERIC_SUFFIX = re.compile(r"\s*[:;,\-\u2013\u2014]\s*(?:a\s+)?(?:novel|memoir|thriller)\s*$", re.IGNORECASE)


def _title_equivalent(expected: str, observed: str) -> bool:
    e, o = normalize(expected), normalize(observed)
    if not e or not o:
        return False
    return e == o or meaningful_words(e) == meaningful_words(o)


_SERIES_PAREN = re.compile(
    r"\s*\(\s*(?P<label>[^()]*?[A-Za-z][^()]*?)\s*(?:[,:#\-]\s*)?"
    r"(?:(?:book|bk|vol\.?|volume|no\.?|number)\s*)?(?P<num>\d+)?\s*\)\s*$",
    re.IGNORECASE,
)


# Qualifiers that name a product or production, not a different text. Anything else in a
# bracket is a series label (needs a number or other evidence) or a real distinction.
_SAME_TEXT_QUALIFIER = re.compile(
    r"(?:the\s+)?(?:full[- ]cast(?:\s+(?:edition|recording|audio))?|dramati[sz]ed(?:\s+edition)?"
    r"|movie\s+tie[- ]in(?:\s+edition)?|tie[- ]in\s+edition|special\s+edition|anniversary\s+edition"
    r"|deluxe\s+edition|collector'?s\s+edition|kindle\s+edition|e-?book(?:\s+edition)?|digital\s+edition)",
    re.IGNORECASE,
)


def series_free_title(value: str, allow_unnumbered: bool = False) -> tuple[str, str]:
    """(title without a trailing "(Series, N)" label, the volume number it carried).

    Used even when Bindery lists no series for the book. A bracket that names an
    edition (graphic novel, abridged, ...) is part of the title and is kept. An
    unnumbered bracket such as "(Vindico)" proves nothing by itself, so it is
    stripped only when the caller has other evidence (a matching ISBN).
    """
    value = str(value or "")
    found = _SERIES_PAREN.search(value)
    if not found or edition_markers(found.group("label")):
        return value, ""
    same_text = bool(_SAME_TEXT_QUALIFIER.fullmatch(found.group("label").strip()))
    if not found.group("num") and not allow_unnumbered and not same_text:
        return value, ""
    base = value[: found.start()].strip()
    if not meaningful_words(base):
        return value, ""
    return base, found.group("num") or ""


def _without_generic_suffix(value: str) -> str:
    return _GENERIC_SUFFIX.sub("", str(value or ""))


def soft_title_match(
    expected: str,
    observed: str,
    series_names=(),
    allow_unnumbered: bool = False,
    allow_unnumbered_expected: bool = False,
) -> bool:
    """Same title, ignoring only a series label (even one Bindery does not list), a leading
    article or a generic "A Novel".

    A substantive subtitle is never ignored: "The English Girl" and "The English
    Girl: Spy Stories" can be different works, and so can two volumes of a saga.
    """
    if not expected or not observed or ebook_title_conflict(expected, observed):
        return False
    # A graphic novel, abridged or illustrated form is a different book from the plain
    # one even when the title words match, so the markers must agree on both sides.
    if edition_markers(expected) != edition_markers(observed):
        return False
    for e in expected_title_variants(expected, series_names) or [expected]:
        for o in expected_title_variants(observed, series_names) or [observed]:
            if _title_equivalent(_without_generic_suffix(e), _without_generic_suffix(o)):
                return True
            (base_e, num_e), (base_o, num_o) = (
                series_free_title(e, allow_unnumbered or allow_unnumbered_expected),
                series_free_title(o, allow_unnumbered))
            if (base_e, base_o) != (e, o) and not (num_e and num_o and num_e != num_o):
                if _title_equivalent(_without_generic_suffix(base_e), _without_generic_suffix(base_o)):
                    return True
    return False


def _people(value: str) -> list[str]:
    value = str(value or "")
    return [part.strip() for part in _PEOPLE.split(value) if part.strip()] or ([value] if value else [])


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "dr", "mr", "mrs", "ms", "esq"}
_NAME_APOSTROPHES = str.maketrans("", "", "'\u2019\u02bc\u2018")


def _parse_person(person: str) -> tuple[list[str], str] | None:
    """(given names, surname) from "Given [Middle] Surname" or "Surname, Given [Middle]"."""
    raw = str(person or "").translate(_NAME_APOSTROPHES)
    if "," in raw:
        surname_part, given_part = raw.split(",", 1)
        surname_tokens = normalize(surname_part).split()
        given = [t for t in normalize(given_part).split() if t not in _NAME_SUFFIXES]
        if not surname_tokens or not given:
            return None
        surname = surname_tokens[-1]
    else:
        tokens = [t for t in normalize(raw).split() if t not in _NAME_SUFFIXES]
        if len(tokens) < 2:
            return None
        surname, given = tokens[-1], tokens[:-1]
    return (given, surname) if len(surname) >= 3 else None


def _given_compatible(a: str, b: str) -> str:
    """"exact", "initial" (one side is just the first letter of the other), or "" (different)."""
    if a == b:
        return "exact"
    if (len(a) == 1 and b.startswith(a)) or (len(b) == 1 and a.startswith(b)):
        return "initial"
    return ""


def author_match_level(expected: str, credits: str) -> str:
    """"exact", "soft" or "" for whether two author credits name the same person.

    Names are compared as given names plus a surname, never as a bag of words:
    the surnames must be equal, the FIRST given names must agree (in full, or as an
    initial), and middle names must agree wherever both sides give one. "soft" means
    an initial stood in for a name or one side omitted a middle name; the caller must
    then require other evidence (the assigned author's name in the book's text).
    A surname alone, and a different person's middle initial, never match.
    """
    if not expected or not credits:
        return ""
    if author_match_strict(expected, credits):
        return "exact"
    best = ""
    for e_person in _people(expected):
        parsed_e = _parse_person(e_person)
        if not parsed_e:
            continue
        for c_person in _people(credits):
            parsed_c = _parse_person(c_person)
            if not parsed_c or parsed_c[1] != parsed_e[1]:
                continue
            given_e, given_c = parsed_e[0], parsed_c[0]
            first = _given_compatible(given_e[0], given_c[0])
            if not first:
                continue
            level = "exact" if first == "exact" else "soft"
            conflict = False
            for x, y in zip(given_e[1:], given_c[1:]):
                middle = _given_compatible(x, y)
                if not middle:
                    conflict = True
                    break
                if middle == "initial":
                    level = "soft"
            if conflict:
                continue
            if len(given_e) != len(given_c):
                level = "soft"
            if level == "exact":
                return "exact"
            best = "soft"
    return best


def soft_author_match(expected: str, credits: str) -> bool:
    return bool(author_match_level(expected, credits))


def same_author(expected: str, credits: str) -> bool:
    """Whether the credits plausibly name the same person; see author_match_level for strength."""
    return bool(author_match_level(expected, credits))


def catalogue_candidates(title: str, author: str, exclude_book_id: int) -> list[dict[str, Any]]:
    """Other Bindery books whose title and author match the given identity."""
    words = sorted(meaningful_words(title), key=len, reverse=True)
    if not words:
        return []
    found: list[dict[str, Any]] = []
    with bindery_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.title, a.name AS author
            FROM books b LEFT JOIN authors a ON a.id = b.author_id
            WHERE lower(b.title) LIKE ? AND b.id != ?
            """,
            (f"%{words[0]}%", int(exclude_book_id or 0)),
        ).fetchall()
        for row in rows:
            if not soft_title_match(str(row["title"] or ""), title) or author_match_level(str(row["author"] or ""), author) != "exact":
                continue
            files = conn.execute(
                "SELECT path, size_bytes FROM book_files WHERE book_id = ? AND format = 'ebook' ORDER BY id LIMIT 5",
                (int(row["id"]),),
            ).fetchall()
            found.append({
                "bookId": int(row["id"]),
                "title": str(row["title"] or ""),
                "author": str(row["author"] or ""),
                "hasFile": bool(files),
                "ebookFiles": [{"path": str(f["path"]), "size": int(f["size_bytes"] or 0)} for f in files],
            })
            if len(found) >= MAX_CANDIDATES:
                break
    return found


def second_pass(result: dict, evidence: dict) -> tuple[str, int] | None:
    """(verdict, confidence) if more evidence settles the book; always records what was tried."""
    expected = evidence.get("expected") or {}
    embedded = evidence.get("embedded") or {}
    content = evidence.get("content") or {}
    expected_signal = content.get("expected_signal") or {}
    embedded_signal = content.get("embedded_signal") or {}
    title, author = str(expected.get("title") or ""), str(expected.get("author") or "")
    file_title, file_author = str(embedded.get("title") or ""), str(embedded.get("author") or "")
    series = [str(n) for n in (result.get("series") or [])]
    tried: list[dict[str, Any]] = []
    evidence["secondPass"] = tried
    evidence["secondPassVersion"] = SECOND_PASS_VERSION
    # What Bindery actually downloaded for this book: a witness that is neither the file nor
    # the catalogue title. Recorded for the panel whether or not it settles the book.
    releases = releases_for_book(int(result.get("book_id") or 0))
    if releases:
        evidence["provenance"] = {
            "releases": [release.to_json() for release in releases[:5]],
            "notABook": [release.raw for release in releases if release.kind == "music"][:3],
        }

    if file_title and file_author:
        isbn_is_assigned_book = bool((evidence.get("isbn") or {}).get("expectedMatch"))
        # An unnumbered label on Bindery's title is accepted when the file's ISBN is one of the
        # book's editions, or when the book's own text prints Bindery's whole title, label included.
        # A label on the file's side needs the ISBN: "The Feros (Vindico)" proves nothing alone.
        title_ok = soft_title_match(
            title, file_title, series,
            allow_unnumbered=isbn_is_assigned_book,
            allow_unnumbered_expected=bool(expected_signal.get("title_found")),
        )
        author_level = author_match_level(author, file_author)
        author_ok = bool(author_level)
        in_text = bool(expected_signal.get("title_found") or expected_signal.get("author_found"))
        # An initial standing in for a name is only the same person if the book itself prints
        # the assigned author's name; an exact match needs nothing more.
        author_backed = author_level == "exact" or bool(expected_signal.get("author_found"))
        tried.append({"check": "tolerantIdentity", "titleMatches": title_ok,
                      "authorMatches": author_level or "no", "foundInBookText": in_text})
        if title_ok and author_ok and in_text and author_backed:
            evidence["explanation"] = (
                "The file's own title and author match this book once initials, name order and "
                "extra credited authors are allowed for, and the book's text contains the title or author."
            )
            if isbn_is_assigned_book:
                evidence["explanation"] += " The file's ISBN is also one of this book's editions in Bindery."
            return "VERIFIED_CORRECT", ISBN_CONFIDENCE if isbn_is_assigned_book else TOLERANT_CONFIDENCE

        proof = provenance_proof(
            expected, embedded,
            {"expectedTitleInText": bool(expected_signal.get("title_found")),
             "expectedAuthorInText": bool(expected_signal.get("author_found")),
             "fileTitleInText": bool(embedded_signal.get("title_found"))},
            releases, author_match_level,
        )
        tried.append({"check": "provenance", "releases": len(releases), "agreed": bool(proof)})
        if proof:
            evidence["proof"] = proof
            evidence["explanation"] = (
                "Three independent sources name the same book: " + "; ".join(proof["sources"])
                + f" (Bindery downloaded it as “{proof['release']}”)."
            )
            return "VERIFIED_CORRECT", PROVENANCE_CONFIDENCE

        expected_in_text = bool(expected_signal.get("title_found") and expected_signal.get("author_found"))
        # The file's ISBN belongs to the assigned book: the first pass already
        # sent conflicting evidence back for review, so it is not overridden here.
        isbn_supports_assigned = bool((evidence.get("isbn") or {}).get("expectedMatch"))
        if (embedded_signal.get("title_found") and not expected_in_text and not (title_ok and author_ok)
                and not isbn_supports_assigned):
            try:
                candidates = catalogue_candidates(file_title, file_author, int(result.get("book_id") or 0))
            except (sqlite3.Error, OSError, ValueError):
                candidates = None
            if candidates is not None:
                tried.append({"check": "catalogueSearch", "matches": len(candidates)})
            if candidates and len(candidates) == 1:
                evidence["actualBook"] = candidates[0]
                evidence["explanation"] = (
                    f"The file's own title and author match \"{candidates[0]['title']}\" "
                    f"(Bindery book {candidates[0]['bookId']}), the book's text supports that "
                    "identity, and the assigned book is not found in it."
                )
                return "WRONG_CONTENT", CATALOGUE_CONFIDENCE
    else:
        tried.append({"check": "fileHasNoTitleOrAuthor"})
    return None

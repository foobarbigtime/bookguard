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
from .title_matching import (
    canon_tokens, clip, designations, edition_markers, meaningful_words, normalize, titles_equal,
)

MAX_CANDIDATES = 5
# Bump when the second pass changes, so earlier second-pass verdicts are re-examined.
SECOND_PASS_VERSION = 3
TOLERANT_CONFIDENCE = 88
ISBN_CONFIDENCE = 95
PROVENANCE_CONFIDENCE = 90
CATALOGUE_CONFIDENCE = 85            # title and author alone name the other book
CATALOGUE_ISBN_CONFIDENCE = 95       # the file's ISBN also names it

_PEOPLE = re.compile(r"\s*(?:;|&|\||\band\b|\bwith\b)\s*", re.IGNORECASE)
_GENERIC_SUFFIX = re.compile(r"\s*[:;,\-\u2013\u2014]\s*(?:a\s+)?(?:novel|memoir|thriller)\s*$", re.IGNORECASE)
# A bracket that names a division of a work, not a series, is never a label to discard.
_DIVISION_WORDS = re.compile(
    r"\b(?:part|parts|volume|vol|book|books|season|chapter|episode|act|canto|level|grade|edition|version"
    r"|issue|disc|disk|cd|tome|section|unit|module|stage|lesson|set|box|number|no)\b", re.IGNORECASE)
_BRACKET_BODY = re.compile(
    r"^(?P<label>.*?)\s*(?:[,:#\-]\s*)?(?:(?:book|bk|vol\.?|volume|no\.?|number)\s*)?(?P<num>\d+)?$",
    re.IGNORECASE)
_RANGE_IN_BODY = re.compile(r"\d\s*(?:[-\u2013\u2014/&]|of|to)\s*\d|\d+\.\d+", re.IGNORECASE)

# Qualifiers that name a product or production, not a different text. Anything else in a
# bracket is a series label (needs a number or other evidence) or a real distinction.
_SAME_TEXT_QUALIFIER = re.compile(
    r"(?:the\s+)?(?:full[- ]cast(?:\s+(?:edition|recording|audio))?|dramati[sz]ed(?:\s+edition)?"
    r"|movie\s+tie[- ]in(?:\s+edition)?|tie[- ]in\s+edition|special\s+edition|anniversary\s+edition"
    r"|deluxe\s+edition|collector'?s\s+edition|kindle\s+edition|e-?book(?:\s+edition)?|digital\s+edition)",
    re.IGNORECASE,
)


def _split_trailing_bracket(value: str) -> tuple[str, str] | None:
    """(text before, text inside) for a title ending in one "(...)" group, without a regex scan."""
    text = clip(value).rstrip()
    if not text.endswith(")"):
        return None
    start = text.rfind("(")
    body = text[start + 1:-1]
    if start <= 0 or "(" in body or ")" in body or len(body) > 80:
        return None
    return text[:start].rstrip(), body.strip()


def series_free_title(value: str, allow_unnumbered: bool = False) -> tuple[str, str]:
    """(title without a trailing "(Series, N)" label, the volume number it carried).

    Used even when Bindery lists no series for the book. Only a bracket that looks like a
    series name is removed: never one naming an edition form (graphic, abridged, ...), a
    division of a work (Part, Act, Season, Level, ...), a range or "N of M", or several
    numbers. An unnumbered series-looking bracket such as "(Vindico)" proves nothing by
    itself, so it is removed only when the caller has other evidence.
    """
    value = clip(value)
    split = _split_trailing_bracket(value)
    if not split:
        return value, ""
    base, body = split
    if not re.search(r"[A-Za-z]", body) or _RANGE_IN_BODY.search(body) or len(re.findall(r"\d+", body)) > 1:
        return value, ""
    parsed = _BRACKET_BODY.match(body)
    same_text = bool(_SAME_TEXT_QUALIFIER.fullmatch(body))
    label = (parsed.group("label") if parsed else body).strip()
    number = (parsed.group("num") if parsed else "") or ""
    if not same_text and (not label or edition_markers(label) or _DIVISION_WORDS.search(label)):
        return value, ""
    if not number and not allow_unnumbered and not same_text:
        return value, ""
    if not meaningful_words(base):
        return value, ""
    return base, number


def _without_generic_suffix(value: str) -> str:
    return _GENERIC_SUFFIX.sub("", clip(value))


def soft_title_match(
    expected: str,
    observed: str,
    series_names=(),
    allow_unnumbered: bool = False,
    allow_unnumbered_expected: bool = False,
) -> bool:
    """Same title, ignoring only a series label (even one Bindery does not list), a leading or
    library-style trailing article, apostrophes, punctuation and a generic "A Novel".

    Word order and every other word must agree. A substantive subtitle is never ignored
    ("The English Girl" vs "The English Girl: Spy Stories"), and volume numbers are compared
    on the ORIGINAL titles, so no label rule can discard a difference in volume.
    """
    expected, observed = clip(expected), clip(observed)
    if not expected or not observed or ebook_title_conflict(expected, observed):
        return False
    # A graphic novel, abridged or illustrated form is a different book from the plain
    # one even when the title words match, so the markers must agree on both sides.
    if edition_markers(expected) != edition_markers(observed):
        return False
    numbers_e, numbers_o = designations(expected), designations(observed)
    if numbers_e and numbers_o and numbers_e != numbers_o:
        return False
    # A range or "N of M" names an omnibus or one part of a set; it equals only the same label.
    if numbers_e != numbers_o and any(n.startswith("range:") for n in numbers_e | numbers_o):
        return False
    for e in expected_title_variants(expected, series_names) or [expected]:
        for o in expected_title_variants(observed, series_names) or [observed]:
            if titles_equal(_without_generic_suffix(e), _without_generic_suffix(o)):
                return True
            base_e, _ = series_free_title(e, allow_unnumbered or allow_unnumbered_expected)
            base_o, _ = series_free_title(o, allow_unnumbered)
            if (base_e, base_o) != (e, o) and titles_equal(
                    _without_generic_suffix(base_e), _without_generic_suffix(base_o)):
                return True
    return False


_PLACEHOLDER_AUTHORS = {
    "unknown", "unknown author", "anonymous", "various", "various authors", "administrator", "admin",
    "microsoft office user", "calibre", "user", "none", "n a", "author", "authors", "editor", "staff",
}


PLACEHOLDER_AUTHORS = _PLACEHOLDER_AUTHORS


def _people(value: str) -> list[str]:
    """The people a credit names. "Martin, Ann M." is ONE person written surname-first, but
    "Ann Martin, Neil Gaiman" is a list, so a comma only inverts a one-word surname."""
    text = clip(value)
    people: list[str] = []
    for part in _PEOPLE.split(text):
        part = part.strip()
        if not part:
            continue
        pieces = [p.strip() for p in part.split(",")]
        pieces = [p for p in pieces if p and normalize(p) not in _NAME_SUFFIXES]
        if len(pieces) == 2 and len(normalize(pieces[0]).split()) == 1:
            people.append(f"{pieces[0]}, {pieces[1]}")
        else:
            people.extend(pieces)
    return people or ([text] if text else [])


_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "dr", "mr", "mrs", "ms", "esq"}
_NAME_APOSTROPHES = str.maketrans("", "", "'\u2019\u02bc\u2018")


def _parse_person(person: str) -> tuple[list[str], str] | None:
    """(given names, surname) from "Given [Middle] Surname" or "Surname, Given [Middle]".

    A hyphenated surname is one word ("Smith-Jones" is not "Jones"); titles and suffixes
    (Dr., Jr., III) are not names."""
    raw = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", "", clip(person)).translate(_NAME_APOSTROPHES)
    if normalize(raw) in _PLACEHOLDER_AUTHORS:
        return None
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
    if not expected or not credits or normalize(expected) in _PLACEHOLDER_AUTHORS or normalize(credits) in _PLACEHOLDER_AUTHORS:
        return ""
    if author_match_strict(clip(expected), clip(credits)):
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
    """Other Bindery books whose title and author match the given identity.

    Every non-excluded book is considered and compared in Python with the same matcher used
    everywhere else (not an SQL LIKE, which is accent-blind and ignores word order), so
    "exactly one other book" really is exactly one. Books the operator excluded in Bindery
    are not candidates. Raises sqlite3.Error if Bindery's database cannot be read.
    """
    tokens = canon_tokens(title)
    if not tokens:
        return []
    needle = max(tokens, key=len)
    found: list[dict[str, Any]] = []
    with bindery_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.title, a.name AS author
            FROM books b LEFT JOIN authors a ON a.id = b.author_id
            WHERE b.excluded = 0 AND b.id != ?
            """,
            (int(exclude_book_id or 0),),
        ).fetchall()
        for row in rows:
            row_title = str(row["title"] or "")
            if needle not in canon_tokens(row_title):
                continue
            if not soft_title_match(row_title, title) or author_match_level(str(row["author"] or ""), author) != "exact":
                continue
            files = conn.execute(
                "SELECT path, size_bytes FROM book_files WHERE book_id = ? AND format = 'ebook' ORDER BY id LIMIT 5",
                (int(row["id"]),),
            ).fetchall()
            found.append({
                "bookId": int(row["id"]),
                "title": row_title,
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
    evidence.pop("secondPassIncomplete", None)
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
        # In a scan (OCR) the author alone is not enough: it could be another book by that author.
        in_text = bool(expected_signal.get("title_found")
                       or (expected_signal.get("author_found") and not expected_signal.get("ocr")))
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

        # Is the file another Bindery book? Only when everything points one way: the file's own
        # title and author are the first thing the book says about itself, the assigned title is
        # nowhere in it, the file's title is not the assigned title either, and the file's ISBN
        # (when it names a Bindery book at all) names the same other book.
        title_is_assigned = soft_title_match(
            title, file_title, series,
            allow_unnumbered=isbn_is_assigned_book,
            allow_unnumbered_expected=bool(expected_signal.get("title_found")),
        )
        isbn_owners = [int(o.get("bookId") or 0) for o in ((evidence.get("isbn") or {}).get("otherOwners") or [])]
        if (embedded_signal.get("strong_identity") and not expected_signal.get("title_found")
                and not expected_signal.get("title_near")        # OCR may have misread the assigned title
                and not title_is_assigned and not isbn_is_assigned_book):
            try:
                candidates: list[dict[str, Any]] | None = catalogue_candidates(
                    file_title, file_author, int(result.get("book_id") or 0))
            except (sqlite3.Error, OSError, ValueError):
                candidates = None
                evidence["secondPassIncomplete"] = True  # Bindery could not be read: look again later
            if candidates is not None:
                tried.append({"check": "catalogueSearch", "matches": len(candidates)})
            if candidates and len(candidates) == 1:
                target = candidates[0]
                if isbn_owners and target["bookId"] not in isbn_owners:
                    tried.append({"check": "isbnNamesAnotherBook"})
                else:
                    corroborated = target["bookId"] in isbn_owners
                    evidence["actualBook"] = target
                    evidence["secondPassCatalogue"] = {"isbnCorroborated": corroborated}
                    evidence["explanation"] = (
                        f"The file's own title and author match \"{target['title']}\" "
                        f"(Bindery book {target['bookId']}), the book opens with that identity, and the "
                        "assigned book is not in it."
                        + (" The file's ISBN also belongs to that book." if corroborated
                           else " Only the title and author name that book, so no ISBN confirms it.")
                    )
                    return "WRONG_CONTENT", CATALOGUE_ISBN_CONFIDENCE if corroborated else CATALOGUE_CONFIDENCE
    else:
        tried.append({"check": "fileHasNoTitleOrAuthor"})
    return None

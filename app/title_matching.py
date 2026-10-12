"""Shared title normalization and number-conflict rules for identity workflows."""

from __future__ import annotations

import re
import unicodedata

from .config import settings

STOPWORDS = {
    "the", "a", "an", "and", "of", "to", "in", "on", "for", "with",
    "book", "bk", "volume", "vol", "part", "edition", "unabridged", "audiobook",
}

_SMALL_NUMBERS = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
    range(20),
))
_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
_NUMBER_WORDS = "|".join([*_SMALL_NUMBERS, *_TENS, "hundred", "thousand", "and"])
NUMBER_LABEL = rf"(?:\d+(?:\.\d+)?|[ivxlcdm]+|(?:{_NUMBER_WORDS})(?:[ -]+(?:{_NUMBER_WORDS}))*)"
_TITLE_DESIGNATION = re.compile(
    rf"\b(part|volume|vol|book|bk)\.?\s*#?\s*({NUMBER_LABEL})(?=\s*[)\]]?\s*$)", re.IGNORECASE,
)


def number_label(value: str) -> str:
    """Canonical digits for numeric, Roman, and English cardinal labels."""
    value = value.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        whole, dot, fraction = value.partition(".")
        fraction = fraction.rstrip("0")
        return str(int(whole)) + (dot + fraction if fraction else "")
    if value and re.fullmatch(r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})", value):
        roman = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
        return str(sum(-roman[c] if i + 1 < len(value) and roman[c] < roman[value[i + 1]] else roman[c]
                       for i, c in enumerate(value)))
    words = value.replace("-", " ").split()
    if not words or words[0] == "and" or words[-1] == "and":
        return ""
    def small(parts: list[str]) -> int | None:
        hundreds = 0
        if len(parts) >= 2 and parts[1] == "hundred" and 1 <= _SMALL_NUMBERS.get(parts[0], 0) <= 9:
            hundreds = _SMALL_NUMBERS[parts[0]] * 100
            parts = parts[2:]
            if parts[:1] == ["and"]:
                parts = parts[1:]
        if not parts:
            return hundreds
        if len(parts) == 1 and parts[0] in {**_SMALL_NUMBERS, **_TENS}:
            return hundreds + {**_SMALL_NUMBERS, **_TENS}[parts[0]]
        if len(parts) == 2 and parts[0] in _TENS and 1 <= _SMALL_NUMBERS.get(parts[1], 0) <= 9:
            return hundreds + _TENS[parts[0]] + _SMALL_NUMBERS[parts[1]]
        return None

    if words.count("thousand") == 1:
        index = words.index("thousand")
        thousands = small(words[:index])
        remainder = words[index + 1:]
        if remainder[:1] == ["and"]:
            remainder = remainder[1:]
        rest = small(remainder)
        return str(thousands * 1000 + rest) if thousands and rest is not None else ""
    number = small(words)
    return str(number) if number is not None else ""


def canonical_title_designations(title: str | None) -> str:
    """Normalize division labels without converting number words in work titles."""
    def replace(match: re.Match) -> str:
        number = number_label(match.group(2))
        label = "Book" if match.group(1).lower() == "bk" else match.group(1)
        return f"{label} {number}" if number else match.group(0)

    return _TITLE_DESIGNATION.sub(replace, title or "")


def normalize(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def meaningful_words(value: str | None) -> set[str]:
    return {word for word in normalize(value).split() if word and word not in STOPWORDS}


def _conflicting_title_number(expected: str, observed: str) -> bool:
    """A number after the same title words identifies a different volume.

    Extra chapter/CD numbers are allowed, but cannot supply a missing book
    number: "NYPD Red 2 - Part 5" must not support "NYPD Red 5".
    """
    ew = [word for word in expected.split() if word not in STOPWORDS]
    mw = [word for word in observed.split() if word not in STOPWORDS]
    for index, number in enumerate(ew):
        if not number.isdecimal() or index == 0:
            continue
        prefix = ew[max(0, index - 3):index]
        if any(word.isdecimal() for word in prefix):
            continue
        for position in range(len(prefix), len(mw)):
            candidate = mw[position]
            if (
                candidate.isdecimal()
                and mw[position - len(prefix):position] == prefix
                and int(candidate) != int(number)
            ):
                return True
    return False


def title_match(expected: str, metadata_text: str) -> bool:
    e = normalize(canonical_title_designations(expected))
    m = normalize(canonical_title_designations(metadata_text))
    if not e or not m:
        return False
    if e == m:
        return True
    if _conflicting_title_number(e, m):
        return False
    if len(e) >= 6 and f" {e} " in f" {m} ":
        return True

    ew = meaningful_words(e)
    mw = meaningful_words(m)

    # Treat harmless leading/trailing article changes and catalogue-style title
    # inversions as equivalent when the meaningful title words are identical.
    # Examples: "Order" == "The Order", "Messenger, The" == "The Messenger".
    if ew and ew == mw:
        return True

    minimum = settings.title_min_shared_words
    # Shared series words are not an identity: "NYPD Red 5" is not
    # "NYPD Red 2", and "All-American Expedition" is not "All-American
    # Murder". Every meaningful expected word (including numbers) must occur.
    if len(ew) >= minimum and ew.issubset(mw):
        return True
    return False


def title_number_conflict(expected: str, observed: str) -> bool:
    """Explicit volume numbers cannot be discarded by catalogue normalization."""
    e, o = normalize(canonical_title_designations(expected)), normalize(canonical_title_designations(observed))
    return _conflicting_title_number(e, o) or _conflicting_title_number(o, e)


_EDITION_MARKERS = {
    "graphic": r"\bgraphic\b", "illustrated": r"\billustrated\b", "abridged": r"\babridged\b",
    "unabridged": r"\bunabridged\b", "adaptation": r"\badaptation\b", "comic": r"\bcomics?\b",
    "manga": r"\bmanga\b", "annotated": r"\bannotated\b", "collection": r"\bcollection\b",
    "omnibus": r"\bomnibus\b", "anthology": r"\banthology\b", "boxset": r"\bbox(?:ed)? set\b",
    "screenplay": r"\bscreenplay\b",
}


def edition_markers(title: str) -> frozenset[str]:
    """Words that make a title a different form of the same story (graphic novel, abridged, ...)."""
    text = normalize(title)
    return frozenset(name for name, pattern in _EDITION_MARKERS.items() if re.search(pattern, text))


# --- shared helpers for the second evidence pass and provenance ------------------------------

MAX_TITLE_CHARS = 300  # titles come from files and release names; bound them before any regex
_ARTICLES = ("the", "an", "a")
_APOSTROPHES = str.maketrans("", "", "'’ʼ‘")
_INVERTED_ARTICLE = re.compile(r",\s*(?:the|an|a)\s*(?=[:(]|$)", re.IGNORECASE)
_BRACKET_GROUP = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_RANGE = re.compile(r"\d\s*(?:[-–—/&]|of|to)\s*\d|\d+\.\d+", re.IGNORECASE)


def clip(value: str | None) -> str:
    return str(value or "")[:MAX_TITLE_CHARS]


def canon_tokens(title: str | None) -> list[str]:
    """Words of a title in order: apostrophes and punctuation gone, a leading article dropped,
    and a library-style trailing article ("Secret Servant, The") treated as absent.

    Word ORDER and every other word are kept, and a title with no Latin letters or digits
    gives no tokens at all (so two such titles are never "equal").
    """
    text = _INVERTED_ARTICLE.sub(" ", clip(title)).translate(_APOSTROPHES)
    tokens = normalize(text).split()
    return tokens[1:] if tokens and tokens[0] in _ARTICLES else tokens


def titles_equal(a: str | None, b: str | None) -> bool:
    ta = canon_tokens(a)
    return bool(ta) and ta == canon_tokens(b)


def designations(title: str | None) -> set[str]:
    """Volume numbers a title names, in brackets or after "Book/Part/Vol/No/#".

    A range ("1-3"), "N of M" or a decimal is kept as one distinct token, so an omnibus
    never equals a single volume.
    """
    text = clip(title)
    found: set[str] = set()
    for group in _BRACKET_GROUP.findall(text):
        body = group[1:-1]
        ranged = _RANGE.search(body)
        if ranged:
            found.add("range:" + re.sub(r"\s+", "", ranged.group(0).lower()))
        else:
            found.update(n.lstrip("0") or "0" for n in re.findall(r"\d+", body))
    outside = _BRACKET_GROUP.sub(" ", text)
    for match in re.finditer(r"(?:#|\b(?:book|part|vol(?:ume)?\.?|no\.?)\s*)(\d+)\b", outside, re.IGNORECASE):
        found.add(match.group(1).lstrip("0") or "0")
    return found

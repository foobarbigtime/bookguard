"""Shared title normalization and number-conflict rules for identity workflows."""

from __future__ import annotations

import re
import unicodedata

from .config import settings

STOPWORDS = {
    "the", "a", "an", "and", "of", "to", "in", "on", "for", "with",
    "book", "volume", "vol", "part", "edition", "unabridged", "audiobook",
}


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
    e = normalize(expected)
    m = normalize(metadata_text)
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
    e, o = normalize(expected), normalize(observed)
    return _conflicting_title_number(e, o) or _conflicting_title_number(o, e)

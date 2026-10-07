from __future__ import annotations

from collections import Counter
import re
from typing import Iterable

from .config import settings
from .title_matching import (
    NUMBER_LABEL,
    meaningful_words,
    normalize,
    title_match as title_match,
    title_number_conflict as title_number_conflict,
)


GENERIC_AUDIO_TITLES = re.compile(
    rf"^(?:(?:chapter|track|disc|disk|cd|part|section|file)\s*(?:{NUMBER_LABEL}|[\divxlc\-_.]*)"
    r"|\d+|prologue|epilogue|preface|foreword|afterword|introduction|opening credits|closing credits)$",
    flags=re.IGNORECASE,
)


def _strict_title_equivalent(expected: str, observed: str) -> bool:
    e = normalize(expected)
    o = normalize(observed)
    if not e or not o:
        return False
    if e == o:
        return True
    ew = meaningful_words(e)
    ow = meaningful_words(o)
    return bool(ew) and ew == ow


def _author_alias_group(expected: str) -> list[str]:
    expected_normalized = normalize(expected)
    if not expected_normalized:
        return []
    for line in settings.author_aliases:
        if "=" not in line:
            continue
        canonical, raw_aliases = line.split("=", 1)
        group = [canonical.strip()] + [part.strip() for part in raw_aliases.split("|")]
        group = [part for part in group if part]
        if expected_normalized in {normalize(part) for part in group}:
            return group
    return [expected]


def _author_candidate_match(candidate: str, credits: str) -> bool:
    e = normalize(candidate)
    c = normalize(credits)
    if not e or not c:
        return False
    if e == c:
        return True
    if len(e) >= 5 and f" {e} " in f" {c} ":
        return True

    # EPUB/PDF/MOBI metadata frequently stores "Surname, Given". After
    # normalization that becomes a token-order difference, not an identity
    # difference. Require the complete token set to match before accepting it.
    e_tokens = e.split()
    c_tokens = c.split()
    if len(e_tokens) >= 2 and len(e_tokens) == len(c_tokens) and set(e_tokens) == set(c_tokens):
        return True

    if not settings.allow_author_surname_match:
        return False
    parts = [p for p in e.split() if len(p) >= 3]
    if not parts:
        return False
    last = parts[-1]
    return len(last) >= 4 and f" {last} " in f" {c} "


def _author_candidate_match_strict(candidate: str, credits: str) -> bool:
    """Match a complete author identity without surname-only fallback."""
    e = normalize(candidate)
    c = normalize(credits)
    if not e or not c:
        return False
    if e == c:
        return True
    e_tokens = e.split()
    c_tokens = c.split()
    return (
        len(e_tokens) >= 2
        and len(e_tokens) == len(c_tokens)
        and set(e_tokens) == set(c_tokens)
    )


def author_match(expected: str, credits: str) -> bool:
    return any(_author_candidate_match(candidate, credits) for candidate in _author_alias_group(expected))


def author_match_strict(expected: str, credits: str) -> bool:
    return any(
        _author_candidate_match_strict(candidate, credits)
        for candidate in _author_alias_group(expected)
    )


def author_mentioned_in_text(expected: str, text: str) -> bool:
    """Find a full configured author/alias in descriptive text without surname-only matching."""
    haystack = normalize(text)
    if not haystack:
        return False
    padded = f" {haystack} "
    for candidate in _author_alias_group(expected):
        needle = normalize(candidate)
        if needle and len(needle) >= 5 and f" {needle} " in padded:
            return True
    return False


def genre_is_music(genre: str | None) -> bool:
    if not genre:
        return False
    normalized_genres = {normalize(x) for x in settings.music_genres}
    g = normalize(genre)
    if g in normalized_genres:
        return True
    raw_parts = re.split(r"[;,/|]+", genre)
    return any(normalize(part) in normalized_genres for part in raw_parts)


def _sample_work_title(sample: dict) -> str:
    album = str(sample.get("album") or "").strip()
    if album and not GENERIC_AUDIO_TITLES.match(album):
        return album
    title = str(sample.get("title") or "").strip()
    if not title or GENERIC_AUDIO_TITLES.match(title):
        return ""
    return title


def _sample_author(sample: dict) -> str:
    placeholders = {"unknown", "unknown author", "author", "authors", "author s",
                    "various", "various artists", "anonymous", "va", "none", "null", "n a"}
    for field in ("author", "album_artist", "artist", "composer"):
        credit = str(sample.get(field) or "").strip()
        key = normalize(credit)
        # A credit needs at least one name-like word: "-", "?" or "1" are
        # uploader placeholders and must not hide a real credit in a later field.
        if key and key not in placeholders and any(len(w) >= 2 and w.isalpha() for w in key.split()):
            return credit
    return ""


def audio_work_identity(sample: dict) -> tuple[str, str]:
    """The work title and author of a track, excluding generic chapter titles."""
    return _sample_work_title(sample), _sample_author(sample)


def catalogue_member_title_match(expected: str, observed: str) -> bool:
    """Accept a work title explicitly named inside a catalogue/box-set title."""
    if title_match(expected, observed):
        return True
    expected_normalized = normalize(expected)
    observed_normalized = normalize(observed)
    # Reverse containment is only evidence for an explicitly listed collection,
    # not for shorter, different titles ("Cross" inside "Cross Justice").
    collection = re.search(r"\b(?:collection|box set|books)\b", expected_normalized) or " / " in expected
    if not collection or not expected_normalized or len(observed_normalized) < 4:
        return False
    return f" {observed_normalized} " in f" {expected_normalized} "


def analyze_audio_identity_set(
    expected_title: str,
    expected_author: str,
    probes: list[dict],
) -> dict:
    """Summarize identity agreement across every readable audio file.

    Representative samples are useful for speed, but they must not be allowed to
    hide a contaminated multi-work directory. This summary is deliberately based
    on the complete verified probe set.
    """
    readable = [probe for probe in probes if not probe.get("probe_error")]
    mismatch_titles: Counter[str] = Counter()
    mismatch_title_display: dict[str, str] = {}

    informative_titles = 0
    informative_authors = 0
    informative_pairs = 0
    title_matches = 0
    author_matches = 0
    pair_matches = 0
    title_mismatches = 0
    foreign_pairs = 0

    for probe in readable:
        title = _sample_work_title(probe)
        author = _sample_author(probe)
        title_supported = bool(title and audio_title_supported(expected_title, {"album": title}))
        author_supported = bool(author and author_match(expected_author, author))

        if title:
            informative_titles += 1
            if title_supported:
                title_matches += 1
            else:
                title_mismatches += 1
                key = normalize(title)
                if key:
                    mismatch_titles[key] += 1
                    mismatch_title_display.setdefault(key, title)

        if author:
            informative_authors += 1
            if author_supported:
                author_matches += 1

        if title and author:
            informative_pairs += 1
            if title_supported and author_supported:
                pair_matches += 1
            elif not title_supported and not author_supported:
                foreign_pairs += 1

    distinct_mismatch_titles = len(mismatch_titles)
    mixed_content = (
        title_mismatches >= 2
        and (
            title_matches > 0
            or distinct_mismatch_titles >= 2
        )
    )

    top_mismatches = [
        {"title": mismatch_title_display[key], "count": count}
        for key, count in mismatch_titles.most_common(10)
    ]

    return {
        "readableCount": len(readable),
        "informativeTitleCount": informative_titles,
        "informativeAuthorCount": informative_authors,
        "informativePairCount": informative_pairs,
        "titleMatchCount": title_matches,
        "authorMatchCount": author_matches,
        "pairMatchCount": pair_matches,
        "titleMismatchCount": title_mismatches,
        "foreignPairCount": foreign_pairs,
        "distinctMismatchTitleCount": distinct_mismatch_titles,
        "mixedContent": mixed_content,
        "hasEmbeddedContradiction": title_mismatches > 0,
        "topMismatchTitles": top_mismatches,
    }


def _has_consensus(values: list[str]) -> bool:
    normalized = [normalize(value) for value in values if normalize(value)]
    if not normalized:
        return False
    _, count = Counter(normalized).most_common(1)[0]
    return (count * 100 / len(normalized)) >= settings.strong_mismatch_consensus_percent


def _strong_mismatch_evidence(
    expected_title: str,
    expected_author: str,
    samples: list[dict],
) -> tuple[bool, str, str]:
    titles: list[str] = []
    authors: list[str] = []
    for sample in samples:
        title = _sample_work_title(sample)
        author = _sample_author(sample)
        if not title or not author:
            continue
        # A single supporting field is enough to block an automatic semantic reject.
        if title_match(expected_title, title) or author_match(expected_author, author):
            return False, "", ""
        titles.append(title)
        authors.append(author)

    minimum = settings.strong_mismatch_min_samples
    if len(titles) < minimum or len(authors) < minimum:
        return False, "", ""
    if not _has_consensus(titles) or not _has_consensus(authors):
        return False, "", ""

    title_key, _ = Counter(normalize(value) for value in titles).most_common(1)[0]
    author_key, _ = Counter(normalize(value) for value in authors).most_common(1)[0]
    detected_title = next(value for value in titles if normalize(value) == title_key)
    detected_author = next(value for value in authors if normalize(value) == author_key)
    return True, detected_title, detected_author


# What may follow a main title in an audio tag: "Cross: Alex Cross, Book 12",
# "The Order: A Novel", "Holly - Unabridged". Only then is the part before the
# separator read as the title on its own.
_AUDIO_SUBTITLE = re.compile(
    r"\b(?:a novel|novel|book\s*\d+|series|unabridged|abridged|volume\s*\d+|vol\.?\s*\d+|part\s*\d+)\b",
    flags=re.IGNORECASE,
)


def _audio_main_title(observed: str) -> str:
    for separator in (":", " - "):
        head, found, rest = observed.partition(separator)
        if found and head.strip() and _AUDIO_SUBTITLE.search(rest):
            return head.strip()
    return ""


def audio_title_supported(expected: str, sample: dict) -> bool:
    """Do this file's tags name the expected title?

    Album and track title are judged on their own as well as together: joined,
    a short title like "Cujo" (album) plus "Chapter 01" (track) no longer looks
    like "Cujo". A generic track title ("Chapter 01", "Track 3") is never
    evidence by itself.
    """
    # Catalogue series suffixes are not part of the work title. Keep numbered
    # main titles intact, and only remove a separately delimited designation.
    from .series_titles import expected_title_variants

    expected_titles = expected_title_variants(expected)
    bare_series = re.fullmatch(r"(.+?)\s*[:]?\s*\([^()]*[a-zA-Z][^()]*\s+\d+\)\s*", expected)
    if bare_series:
        expected_titles.append(bare_series.group(1).rstrip(" :"))

    album = str(sample.get("album") or "").strip()
    track = str(sample.get("title") or "").strip()
    if album and GENERIC_AUDIO_TITLES.match(album):
        album = ""
    if track and GENERIC_AUDIO_TITLES.match(track):
        track = ""
    candidates = [value for value in (album, track, " ".join(filter(None, [album, track]))) if value]
    for value in candidates:
        if catalogue_member_title_match(expected, value):
            return True
        for title in expected_titles:
            if title_match(title, value):
                return True
            head = _audio_main_title(value)
            if head and _strict_title_equivalent(title, head):
                return True
    return False


def classify_audio(
    expected_title: str,
    expected_author: str,
    samples: list[dict],
) -> tuple[str, int, str, list[str]]:
    readable = [sample for sample in samples if not sample.get("probe_error")]
    if not readable:
        return "REVIEW", 55, "NO_METADATA", ["No readable audio metadata was found."]

    any_title = False
    any_author = False
    any_music = False
    any_pair = False

    for sample in readable:
        title_text = " ".join(filter(None, [sample.get("album"), sample.get("title")]))
        credits = " ".join(
            filter(
                None,
                [
                    sample.get("artist"),
                    sample.get("album_artist"),
                    sample.get("author"),
                    sample.get("composer"),
                ],
            )
        )
        title_supported = audio_title_supported(expected_title, sample)
        # Some commercial audiobooks tag the narrator as Artist while embedding
        # the author's full name in Album. Accept the full author/alias there,
        # but deliberately do not use surname-only matching in descriptive text.
        author_supported = (
            author_match(expected_author, credits)
            or author_mentioned_in_text(expected_author, title_text)
        )
        any_title = any_title or title_supported
        any_author = any_author or author_supported
        any_pair = any_pair or (title_supported and author_supported)
        any_music = any_music or genre_is_music(sample.get("genre"))

    # A music file can coincidentally share a book title. If it is explicitly
    # music-like and there is no author support, title alone is not enough to
    # save it from rejection.
    if settings.reject_music_mismatch and any_music and not any_author:
        return "REJECT", 100, "MUSIC_MISMATCH", [
            "Metadata looks like music and does not match the expected author; a title-only coincidence is not treated as audiobook evidence."
        ]

    whole_set = analyze_audio_identity_set(expected_title, expected_author, readable)
    if whole_set["mixedContent"]:
        return "REVIEW", 80, "MIXED_AUDIO_CONTENT", [
            "Readable audio files identify multiple works that do not all match the Bindery assignment."
        ]
    if any_title and any_author:
        if whole_set["hasEmbeddedContradiction"] or not any_pair:
            return "REVIEW", 65, "CONFLICTING_AUDIO_IDENTITY", [
                "Audio metadata has conflicting work titles or only supports the expected title and author in different files."
            ]
        return "PASS", 5, "MATCH", [
            "Embedded title and author metadata support the Bindery assignment."
        ]

    if (
        settings.reject_strong_mismatch
        and not any_music
        and not any_title
        and not any_author
    ):
        strong, detected_title, detected_author = _strong_mismatch_evidence(
            expected_title, expected_author, readable
        )
        if strong:
            return "REJECT", 95, "STRONG_MISMATCH", [
                f'Embedded metadata consistently identifies "{detected_title}" by '
                f'"{detected_author}", not the expected book and author.'
            ]

    if not any_title and not any_author:
        return "REVIEW", 80, "MISMATCH", [
            "Neither title nor author metadata matches the Bindery assignment."
        ]
    if any_title:
        return "REVIEW", 35, "PARTIAL_MATCH", [
            "Title metadata matches, but author metadata does not."
        ]
    return "REVIEW", 40, "PARTIAL_MATCH", [
        "Author metadata matches, but title metadata does not."
    ]


def classify_ebook(
    expected_title: str,
    expected_author: str,
    metadata: dict,
    *,
    series_names: Iterable[str] = (),
) -> tuple[str, int, str, list[str]]:
    from .series_titles import ebook_title_match

    title = str(metadata.get("title") or "")
    author = str(metadata.get("author") or "")
    tm = ebook_title_match(expected_title, title, series_names)
    am = author_match(expected_author, author)
    if tm and am:
        return "PASS", 5, "MATCH", [
            "Embedded ebook title and author support the Bindery assignment."
        ]

    # Some ebook files have their title and creator fields accidentally swapped.
    # Only accept this when both cross-field matches are strict so a loose title
    # overlap or surname-only author match cannot turn a real mismatch into PASS.
    if (
        title
        and author
        and author_match_strict(expected_author, title)
        and _strict_title_equivalent(expected_title, author)
    ):
        return "PASS", 10, "SWAPPED_METADATA", [
            "Embedded ebook title and author fields appear to be swapped, but both identify the expected book."
        ]

    if tm:
        return "REVIEW", 35, "PARTIAL_MATCH", [
            "Ebook title matches, but embedded author does not."
        ]
    if am:
        return "REVIEW", 40, "PARTIAL_MATCH", [
            "Ebook author matches, but embedded title does not."
        ]
    if title or author:
        return "REVIEW", 80, "MISMATCH", [
            "Embedded ebook title/author do not match the Bindery assignment."
        ]
    return "REVIEW", 50, "NO_METADATA", [
        "No usable embedded ebook title/author metadata was found."
    ]

from __future__ import annotations

from collections import Counter
import re
import unicodedata

from .config import settings


STOPWORDS = {
    "the", "a", "an", "and", "of", "to", "in", "on", "for", "with",
    "book", "volume", "vol", "part", "edition", "unabridged", "audiobook",
}

GENERIC_AUDIO_TITLES = re.compile(
    r"^(?:chapter|track|disc|disk|cd|part|section|file)\s*[\divxlc\-_.]*$|^\d+$",
    flags=re.IGNORECASE,
)


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


def title_match(expected: str, metadata_text: str) -> bool:
    e = normalize(expected)
    m = normalize(metadata_text)
    if not e or not m:
        return False
    if e == m:
        return True
    if len(e) >= 6 and f" {e} " in f" {m} ":
        return True
    ew = meaningful_words(e)
    mw = meaningful_words(m)
    shared = ew & mw
    minimum = settings.title_min_shared_words
    if len(ew) >= minimum and len(shared) >= minimum:
        return True
    if len(ew) == 1 and e == m:
        return True
    return False


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
    if not settings.allow_author_surname_match:
        return False
    parts = [p for p in e.split() if len(p) >= 3]
    if not parts:
        return False
    last = parts[-1]
    return len(last) >= 4 and f" {last} " in f" {c} "


def author_match(expected: str, credits: str) -> bool:
    return any(_author_candidate_match(candidate, credits) for candidate in _author_alias_group(expected))


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
    if album:
        return album
    title = str(sample.get("title") or "").strip()
    if not title or GENERIC_AUDIO_TITLES.match(title):
        return ""
    return title


def _sample_author(sample: dict) -> str:
    return str(
        sample.get("author")
        or sample.get("album_artist")
        or sample.get("artist")
        or ""
    ).strip()


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
        any_title = any_title or title_match(expected_title, title_text)
        any_author = any_author or author_match(expected_author, credits)
        any_music = any_music or genre_is_music(sample.get("genre"))

    if any_title and any_author:
        return "PASS", 5, "MATCH", [
            "Embedded title and author metadata support the Bindery assignment."
        ]

    if settings.reject_music_mismatch and any_music and not any_title and not any_author:
        return "REJECT", 100, "MUSIC_MISMATCH", [
            "Metadata looks like music and does not match the expected book or author."
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
) -> tuple[str, int, str, list[str]]:
    title = str(metadata.get("title") or "")
    author = str(metadata.get("author") or "")
    tm = title_match(expected_title, title)
    am = author_match(expected_author, author)
    if tm and am:
        return "PASS", 5, "MATCH", [
            "Embedded ebook title and author support the Bindery assignment."
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

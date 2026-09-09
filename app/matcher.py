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


def author_match(expected: str, credits: str) -> bool:
    e = normalize(expected)
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


def genre_is_music(genre: str | None) -> bool:
    if not genre:
        return False
    normalized_genres = {normalize(x) for x in settings.music_genres}
    g = normalize(genre)
    if g in normalized_genres:
        return True
    raw_parts = re.split(r"[;,/|]+", genre)
    return any(normalize(part) in normalized_genres for part in raw_parts)


def classify_audio(expected_title: str, expected_author: str, samples: list[dict]) -> tuple[str, int, list[str]]:
    if not samples:
        return "REVIEW", 75, ["No readable audio metadata was found."]

    any_title = False
    any_author = False
    any_music = False

    for sample in samples:
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
        return "PASS", 5, ["Embedded title and author metadata support the Bindery assignment."]
    if settings.reject_music_mismatch and any_music and not any_title and not any_author:
        return "REJECT", 100, ["Metadata looks like music and does not match the expected book or author."]
    if not any_title and not any_author:
        return "REVIEW", 80, ["Neither title nor author metadata matches the Bindery assignment."]
    if any_title:
        return "REVIEW", 35, ["Title metadata matches, but author metadata does not."]
    return "REVIEW", 40, ["Author metadata matches, but title metadata does not."]


def classify_ebook(expected_title: str, expected_author: str, metadata: dict) -> tuple[str, int, list[str]]:
    title = metadata.get("title", "")
    author = metadata.get("author", "")
    tm = title_match(expected_title, title)
    am = author_match(expected_author, author)
    if tm and am:
        return "PASS", 5, ["Embedded ebook title and author support the Bindery assignment."]
    if tm:
        return "REVIEW", 35, ["Ebook title matches, but embedded author does not."]
    if am:
        return "REVIEW", 40, ["Ebook author matches, but embedded title does not."]
    if title or author:
        return "REVIEW", 80, ["Embedded ebook title/author do not match the Bindery assignment."]
    return "REVIEW", 65, ["No usable embedded ebook title/author metadata was found."]

"""Check Bindery's unmatched files: junk, not in the library, or which book.

Bindery's library scan lists files it could not place under Import → "In your
library". Its suggestions are title look-alikes ("Katt vs. Dogg 80%"), which
are never accepted as proof here. BookGuard runs its own checks on each file
and gives one plain verdict with the evidence:

* JUNK: a hard failure (not the type it claims, broken, locked, nearly empty,
  unsafe, silent, a sample, or an exact copy of a file the library has).
* NOT_IN_LIBRARY: a real book whose own identity (embedded metadata, ISBN
  lookup, file and folder names) agrees from at least two sources, and no
  Bindery book matches it.
* BELONGS: at least two independent sources point at the same Bindery book,
  and that book has no file of this kind yet (the only case Attach offers).
* DUPLICATE: proven to be a library book that already has a file of this kind.
* OTHER_LANGUAGE: proven to be a library book, but a translation of it.
* UNSURE: real, but BookGuard cannot prove what it is.

Read-only: nothing moves. Attach uses Bindery's own adopt (which Bindery can
undo); quarantine for unmatched files comes in a later step.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading
from typing import Any
import unicodedata
import zipfile

from .actions import ActionError, resolve_bindery_api_key
from .audiobook_verification import cached_or_probe_audio_file, disc_track_sequence_warnings
from .bindery_client import BinderyClient, BinderyClientError
from .config import settings
from .db import bindery_conn, bindery_files_for_book, local_conn
from .ebook_extraction import extract_ebook_identity
from .ebook_security import inspect_ebook_security
from .file_safety import sha256_file
from .isbn_evidence import file_isbns
from .matcher import analyze_audio_identity_set, audio_work_identity
from .pdf_probe import probe_pdf
from .scanner import map_path
from .series_titles import ebook_title_conflict
from .title_matching import canonical_title_designations, title_number_conflict

VERDICTS = {
    "JUNK": "Junk",
    "NOT_IN_LIBRARY": "Not in your library",
    "BELONGS": "Belongs to a book",
    "DUPLICATE": "Extra copy",
    "OTHER_LANGUAGE": "Another-language edition",
    "UNSURE": "Unsure",
}
EBOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw", ".azw3", ".txt", ".rtf", ".cbz"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wma", ".wav"}
MIN_BOOK_WORDS = 300  # fewer readable words than this is not a book
SAMPLE_MAX_WORDS = 25000  # a "free sample" notice in a text this short means a sample
MIN_AUDIO_SECONDS = 5 * 60
SAMPLE_TEXT = re.compile(
    r"(this is a (free )?sample|end of (this )?(free )?sample|you have reached the end of (this|the) (preview|sample)"
    r"|buy (now|the (full|complete) (book|ebook))|to (continue|keep) reading,? (purchase|buy))",
    re.IGNORECASE,
)
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
STOPWORDS = {
    "English": {"the", "and", "of", "to", "was", "he", "she", "that", "with", "his", "her", "you"},
    "Dutch": {"de", "het", "een", "en", "van", "ik", "niet", "dat", "zijn", "je", "hij", "met"},
    "German": {"der", "die", "und", "das", "nicht", "ich", "sie", "ist", "mit", "ein", "zu", "auf"},
    "French": {"le", "la", "les", "et", "des", "une", "est", "pas", "que", "il", "elle", "dans"},
    "Spanish": {"el", "la", "los", "las", "que", "y", "una", "por", "con", "no", "se", "del"},
    "Swedish": {"och", "att", "det", "som", "en", "på", "är", "av", "för", "med", "inte", "jag"},
}
LANGUAGE_CODES = {
    "English": {"en", "eng", "english"},
    "Dutch": {"nl", "nld", "dut", "dutch"},
    "German": {"de", "deu", "ger", "german"},
    "French": {"fr", "fra", "fre", "french"},
    "Spanish": {"es", "spa", "spanish"},
    "Swedish": {"sv", "swe", "swedish"},
}
# Genre tags that mean a music album; checked as whole tags so "Science Fiction" never counts.
MUSIC_GENRES = {
    "rock", "pop", "country", "jazz", "blues", "folk", "classical", "hip hop", "hiphop", "rap", "soundtrack",
    "r b", "rnb", "soul", "metal", "punk", "electronic", "dance", "reggae", "alternative", "indie", "americana",
    "bluegrass", "gospel", "singer songwriter", "latin", "disco", "funk", "house", "techno", "ambient",
    "new age", "easy listening", "oldies", "alt country", "country folk", "folk rock", "soft rock",
    "progressive rock", "classic rock", "hard rock", "pop rock", "indie rock", "country rock", "punk rock",
    "classic country", "rock n roll", "rock and roll", "heavy metal", "smooth jazz", "rhythm and blues",
}

_LOCK = threading.Lock()
_STATUS: dict[str, Any] = {"running": False, "checked": 0, "total": 0, "error": "", "finishedAt": ""}


# ---- storage -----------------------------------------------------------------

def init_unmatched_db() -> None:
    with local_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS unmatched_checks (
                row_id INTEGER PRIMARY KEY,
                signature TEXT NOT NULL,
                kind TEXT NOT NULL,
                format TEXT NOT NULL,
                rel_path TEXT NOT NULL,
                root_path TEXT NOT NULL,
                name TEXT NOT NULL,
                verdict TEXT NOT NULL,
                reason TEXT NOT NULL,
                book_id INTEGER,
                book_title TEXT,
                evidence_json TEXT NOT NULL,
                checked_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def stored_checks() -> list[dict]:
    init_unmatched_db()
    with local_conn() as conn:
        rows = conn.execute("SELECT * FROM unmatched_checks ORDER BY verdict, name").fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        item["verdictLabel"] = VERDICTS.get(item["verdict"], item["verdict"])
        items.append(item)
    return items


def _save(row_id: int, signature: str, item: dict, outcome: dict) -> None:
    with local_conn() as conn:
        conn.execute(
            """
            INSERT INTO unmatched_checks(row_id, signature, kind, format, rel_path, root_path, name, verdict,
                reason, book_id, book_title, evidence_json, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(row_id) DO UPDATE SET signature=excluded.signature, kind=excluded.kind,
                format=excluded.format, rel_path=excluded.rel_path, root_path=excluded.root_path,
                name=excluded.name, verdict=excluded.verdict, reason=excluded.reason, book_id=excluded.book_id,
                book_title=excluded.book_title, evidence_json=excluded.evidence_json, checked_at=excluded.checked_at
            """,
            (
                row_id, signature, str(item.get("kind") or ""), str(item.get("format") or ""),
                str(item.get("relPath") or ""), str(item.get("rootPath") or ""),
                str(item.get("parsedTitle") or PurePosixPath(str(item.get("relPath") or "")).name),
                outcome["verdict"], outcome["reason"], outcome.get("bookId"), outcome.get("bookTitle"),
                json.dumps(outcome["evidence"]), datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


# ---- text helpers ------------------------------------------------------------

def _norm(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"['`]", "", text)  # "Caliban's" and "Calibans" are the same word
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"^(the|a|an) ", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


JUNK_AUTHORS = {"author", "authors", "unknown", "unknown author", "various", "various artists", "anonymous", "va"}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md"}


def _name_words(name: str) -> list[str]:
    text = _norm(name)
    if text in JUNK_AUTHORS:
        return []
    return [w for w in text.split() if len(w) >= 2 and not w.isdigit() and w not in NAME_SUFFIXES]  # drops initials


def _surname(author: str) -> str:
    """The first author's family name, in any of the usual orders:
    "J.K. Rowling", "Rowling J.K.", "Corey, James S.A." and "Patterson, James"."""
    first = re.split(r";|&|/| and ", str(author or ""))[0]
    parts = [p for p in first.split(",") if p.strip()]
    if not parts:
        return ""
    words = _name_words(parts[0])  # "A, B" is "Surname, Given" when A is one word, else two authors
    return words[-1] if words else ""


GENERIC_FOLDER = re.compile(
    r"^(e-?books?|audio-?books?|_?audio|m4b|mp3|epub|pdf|mobi|us|uk|au|ca|unabridged|abridged|editions?\s*\d*"
    r"|(cd|dis[ck]|part)\s*\d+|.*\brecovered\b.*|(primary|alternate|alternative|original|other|first|second)\s+editions?.*"
    r"|\S*~\S*)$",  # "abook.ws~JsPn-TeBkBk-2017": a release tag, not a title
    re.IGNORECASE,
)
_SERIES_PREFIXES = (
    re.compile(r"^\s*\[[^\]]*\]\s*-?\s*"),  # "[Alex Cross 23] - "
    re.compile(r"^\s*(book|vol(ume)?|part|no\.?|#)?\s*\d+(\.\d+)?\s*[-.)]?\s+", re.IGNORECASE),  # "05 - ", "Book 5 - ", "1993 - "
    re.compile(r"^\s*[a-z]{1,5}\s*\d+(\.\d+)?\s*-?\s+", re.IGNORECASE),  # "TDT 0.5 ", "WMC 01 - ", "LS1 ", "MR 3 - "
    re.compile(r"^.{1,40}?\s#?\d+(\.\d+)?\s+-\s+"),  # "Expanse 01 - ", "Maximum Ride 03 - "
)
_EDITION_SUFFIX = re.compile(r"[\s_-]+(uk|us|unabridged|abridged)$", re.IGNORECASE)


def _title_keys(title: str, *, numbered: bool = False) -> set[str]:
    """Every fair reading of a title: as given, before a ": Series, Book N" subtitle, the
    last " - " part, and each without series or number prefixes ("05 - ", "TDT 0.5 ").
    Two titles name the same book when their readings share one. With numbered, also
    without a trailing number ("In the Tall Grass 1"): only for comparing one file's own
    sources, never library titles ("Alpha 2" is not "Alpha")."""
    raw = canonical_title_designations(str(title or ""))
    forms = {raw, raw.split(":")[0]}
    parts = [p for p in re.split(r"\s+-\s+", raw) if p.strip()]
    if len(parts) >= 2:
        forms.add(parts[-1])
    for form in list(forms):
        stripped = _EDITION_SUFFIX.sub("", form)
        for _ in range(2):
            for pattern in _SERIES_PREFIXES:
                stripped = pattern.sub("", stripped, count=1)
        forms.update({stripped, _EDITION_SUFFIX.sub("", form)})
        if numbered:
            forms.add(re.sub(r"\s+\d{1,2}$", "", stripped))
    return {key for key in (_norm(form) for form in forms) if len(key) >= 2}


def _words(text: str) -> int:
    return len(re.findall(r"[^\W\d_]{2,}", text or ""))


def text_language(text: str) -> str:
    """Best guess from common words; "" when there is too little text to tell."""
    tokens = re.findall(r"[^\W\d_]+", (text or "").lower())[:5000]
    if len(tokens) < 200:
        return ""
    scores = {lang: sum(1 for t in tokens if t in words) for lang, words in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 0.05 * len(tokens) else ""


_PART = re.compile(r"[\s._-]*(\(?\d+\s*(-{1,2}|of|/)\s*\d+\)?|part\s*\d+|cd\s*\d+|disc\s*\d+|\d{1,3})$", re.IGNORECASE)


def name_identities(name: str) -> list[tuple[str, str]]:
    """(title, author) readings of an 'X - Y' file name, both orders: releases use either."""
    stem = _PART.sub("", PurePosixPath(name).stem).strip()
    parts = [p.strip() for p in re.split(r"\s+-\s+", stem) if p.strip()]
    if len(parts) != 2:
        return []
    return [(parts[1], parts[0]), (parts[0], parts[1])]


NAME_SOURCES = {"File name", "Folder names"}  # one uploader often names both: never proof on their own


def _place(position: str) -> float | None:
    try:
        return float(str(position).strip())
    except ValueError:
        return None


# ---- the library -------------------------------------------------------------

def library_books() -> list[dict]:
    """The books Bindery shows: books you excluded are hidden everywhere in Bindery, so
    they are not in the library here either (and nothing is ever attached to one)."""
    with bindery_conn() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
        hidden = "WHERE b.excluded = 0" if "excluded" in columns else ""
        language = "b.language" if "language" in columns else "'' AS language"
        rows = conn.execute(
            f"SELECT b.id, b.title, {language}, a.name AS author FROM books b "
            f"JOIN authors a ON a.id = b.author_id {hidden}"
        ).fetchall()
        books = {int(row["id"]): {**dict(row), "series": []} for row in rows}
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"series", "series_books"} <= tables:
            for row in conn.execute("SELECT sb.book_id, s.title, sb.position_in_series FROM series_books sb "
                                    "JOIN series s ON s.id = sb.series_id"):
                if int(row["book_id"]) in books:
                    books[int(row["book_id"])]["series"].append((_norm(row["title"]), str(row["position_in_series"])))
    return list(books.values())


class Library:
    def __init__(self, books: list[dict]):
        self.books = {int(b["id"]): b for b in books}
        self.by_title: dict[str, list[dict]] = defaultdict(list)
        self.exact: dict[str, list[dict]] = defaultdict(list)
        for book in books:
            self.exact[_norm(book["title"])].append(book)
            for key in _title_keys(book["title"]):
                self.by_title[key].append(book)

    def same_title(self, title: str) -> list[dict]:
        found: dict[int, dict] = {}
        for key in _title_keys(title):
            for book in self.by_title.get(key, []):
                if not ebook_title_conflict(book["title"], title):
                    found[int(book["id"])] = book
        return list(found.values())

    def find(self, title: str, author: str) -> tuple[dict | None, list[dict]]:
        """(the book, []) or (None, the books it could be when it fits more than one)."""
        surname = _surname(author)
        if not surname:
            return None, []
        exact = [b for b in self.exact.get(_norm(title), [])
                 if _surname(b["author"]) == surname and not ebook_title_conflict(b["title"], title)]
        if len(exact) == 1:
            return exact[0], []
        if exact:
            return None, sorted(exact, key=lambda b: b["id"])
        # A shortened reading ("Chronicles" for "Chronicles: First") counts only when it
        # fits exactly one of that author's books; otherwise it could attach to the wrong one.
        loose = sorted((b for b in self.same_title(title) if _surname(b["author"]) == surname), key=lambda b: b["id"])
        return (loose[0], []) if len(loose) == 1 else (None, loose)

    def match(self, title: str, author: str) -> dict | None:
        return self.find(title, author)[0]

    def series_book(self, title: str, author: str) -> dict | None | bool:
        """For a "Series NN" title of one of the author's Bindery series: the book at that
        place (when exactly one), False when the series is theirs but the place is not
        clear, None when it is not one of their series at all."""
        found = re.fullmatch(r"(.*[a-z])\s+(\d{1,3})", _norm(title))
        surname = _surname(author)
        if not found or not surname:
            return None
        name, place = found.group(1), float(found.group(2))
        theirs = [b for b in self.books.values() if _surname(b["author"]) == surname
                  and any(series == name for series, _ in b.get("series") or [])]
        if not theirs:
            return None
        at = [b for b in theirs if any(series == name and _place(position) == place
                                       for series, position in b.get("series") or [])]
        return at[0] if len(at) == 1 else False


# ---- file checks -------------------------------------------------------------

def _drm_locked(path: Path) -> bool:
    if path.suffix.lower() != ".epub":
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "META-INF/rights.xml" in names:
                return True
            if "META-INF/encryption.xml" not in names:
                return False
            xml = archive.read("META-INF/encryption.xml").decode("utf-8", "ignore")
    except (OSError, zipfile.BadZipFile):
        return False
    algorithms = set(re.findall(r'Algorithm="([^"]+)"', xml))
    return bool(algorithms - FONT_OBFUSCATION)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MIN_PICTURE_PAGES = 5


def _picture_pages(path: Path) -> int | None:
    """Page count for formats that may be all pictures (comics, scanned PDFs); None otherwise."""
    suffix = path.suffix.lower()
    if suffix == ".cbz":
        try:
            with zipfile.ZipFile(path) as archive:
                return sum(1 for name in archive.namelist() if PurePosixPath(name).suffix.lower() in IMAGE_EXTENSIONS)
        except (OSError, zipfile.BadZipFile):
            return 0
    if suffix == ".pdf":
        pages = probe_pdf(path).get("pages")
        return int(pages) if isinstance(pages, int) else None
    return None


def _silent(path: str) -> bool | None:
    """True when a 60-second sample from a quarter in is all silence; None if ffmpeg can't tell."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-ss", "120", "-t", "60", "-i", path,
             "-af", "silencedetect=n=-50dB:d=55", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return "silence_duration" in proc.stderr


def _check(evidence: list, name: str, result: str, detail: str) -> None:
    evidence.append({"check": name, "result": result, "detail": detail})


def _ebook_facts(path: Path, evidence: list) -> dict:
    facts: dict[str, Any] = {"junk": "", "identities": [], "front": "", "isbns": []}
    if path.stat().st_size < 4096:
        facts["junk"] = f"The file is only {path.stat().st_size} bytes; no ebook is that small."
    security = inspect_ebook_security(
        path,
        check_malware=settings.verification_malware_scan,
        clamd_host=settings.verification_clamd_host,
        clamd_port=settings.verification_clamd_port,
        malware_timeout_seconds=settings.verification_malware_timeout_seconds,
        malware_max_bytes=settings.verification_malware_max_bytes,
    )
    failed = [name for name in security["failures"] if not (name == "malwareScan" and security["inconclusive"])]
    if failed:
        reasons = "; ".join(str(security["checks"][name].get("message") or name) for name in failed)
        _check(evidence, "File and safety checks", "fail", reasons)
        facts["junk"] = facts["junk"] or f"It failed the file checks: {reasons}"
        return facts
    _check(evidence, "File and safety checks", "pass", "Type, structure and enabled safety checks passed.")
    if _drm_locked(path):
        _check(evidence, "DRM", "fail", "The ebook is locked (DRM), so it can't be read.")
        facts["junk"] = facts["junk"] or "It is locked by DRM, so it can't be read."
        return facts
    try:
        identity = extract_ebook_identity(str(path))
    except Exception as exc:  # unreadable content is a finding, not a crash
        _check(evidence, "Text", "fail", f"No readable text: {exc}")
        facts["junk"] = facts["junk"] or "BookGuard could not read any text from it."
        return facts
    words = _words(identity.text)
    pages = _picture_pages(path) if words < MIN_BOOK_WORDS else None
    if pages is not None:  # comics and scanned PDFs are pictures, not prose
        if pages < MIN_PICTURE_PAGES:
            _check(evidence, "Length", "fail", f"Only {pages} pages and no readable text.")
            facts["junk"] = facts["junk"] or f"It has only {pages} pages and no readable text; that is not a book."
        else:
            _check(evidence, "Length", "pass", f"{pages} pages of pictures (no text layer).")
    elif words < MIN_BOOK_WORDS:
        _check(evidence, "Length", "fail", f"Only {words} readable words.")
        facts["junk"] = facts["junk"] or f"It has only {words} readable words; that is not a book."
    else:
        _check(evidence, "Length", "pass", f"About {words:,} readable words.")
    if SAMPLE_TEXT.search(identity.text or "") and MIN_BOOK_WORDS <= words < SAMPLE_MAX_WORDS:
        _check(evidence, "Sample text", "fail", "The text says it is a sample or preview.")
        facts["junk"] = facts["junk"] or "It is a sample or preview, not the full book."
    language = text_language(identity.text)
    if language:
        _check(evidence, "Language of the text", "info", language)
    facts["language"] = language
    metadata = identity.metadata or {}
    if metadata.get("title"):
        facts["identities"].append(("Embedded metadata", metadata.get("title"), metadata.get("author") or ""))
    facts["front"] = _norm((identity.front_text or identity.text or "")[:6000])
    facts["isbns"] = file_isbns(identity.identifiers)
    return facts


def _audio_facts(files: list[Path], evidence: list) -> dict:
    facts: dict[str, Any] = {"junk": "", "identities": [], "front": "", "isbns": []}
    # Identity authorization covers the complete unit, not a display sample.
    probes = [cached_or_probe_audio_file(str(f))[0] for f in files]
    facts["audio_probes"] = probes
    broken = [PurePosixPath(p["path"]).name for p in probes if p.get("probe_error") or not p.get("audio_stream_count")]
    if broken:
        _check(evidence, "Audio files", "fail", f"Can't be read as audio: {', '.join(broken[:5])}")
        facts["junk"] = f"{len(broken)} of {len(probes)} files can't be read as audio."
        return facts
    total = sum(float(p.get("duration_seconds") or 0) for p in probes)
    if total < MIN_AUDIO_SECONDS:
        _check(evidence, "Length", "fail", f"Only {total / 60:.1f} minutes of audio.")
        facts["junk"] = f"It has only {total / 60:.1f} minutes of audio; that is not an audiobook."
    else:
        _check(evidence, "Length", "pass", f"{total / 3600:.1f} hours of audio in {len(probes)} files.")
    genres = [str(p.get("genre") or "") for p in probes]
    music = [g for g in genres if _music_genre(g)]
    if music and len(music) * 2 > len(probes):
        _check(evidence, "Genre", "fail", f"The files are tagged as music (“{music[0]}”).")
        facts["junk"] = facts["junk"] or f"It is music (genre “{music[0]}”), not an audiobook."
    elif any(genres):
        _check(evidence, "Genre", "info", next(g for g in genres if g))
    warnings = disc_track_sequence_warnings(probes)
    if warnings:
        _check(evidence, "All parts present", "warn", "; ".join(warnings[:3]))
    else:
        _check(evidence, "All parts present", "pass", "Track numbering has no gaps or repeats.")
    samples = [str(files[0]), str(files[len(files) // 2])] if len(files) > 1 else [str(files[0])]
    silent = [_silent(path) for path in dict.fromkeys(samples)]
    if silent and all(s is True for s in silent):
        _check(evidence, "Silence", "fail", "The sampled audio is silent.")
        facts["junk"] = facts["junk"] or "The audio is silent."
    elif any(s is False for s in silent):
        _check(evidence, "Silence", "pass", "The sampled audio has sound.")
    seen: set[tuple[str, str]] = set()
    for probe in probes:
        title, author = audio_work_identity(probe)
        if title and (title, author) not in seen:
            facts["identities"].append(("Audio tags", title, author))
            seen.add((title, author))
    return facts


def _music_genre(genre: str) -> bool:
    """Only whole, known music genres: "Folk Horror" or "Pop Psychology" are books."""
    return any(_norm(part) in MUSIC_GENRES for part in re.split(r"[,;/|]", genre or ""))


# ---- verdict -----------------------------------------------------------------

def _local_path(bindery_path: str, fmt: str) -> str:
    """Map by the library folder the file is in, not by its type: Bindery lists, say,
    a .txt "ebook" that sits in the audiobooks folder."""
    matches = []
    for root_fmt, prefix in (("audiobook", settings.audiobook_bindery_prefix), ("ebook", settings.ebook_bindery_prefix)):
        prefix = prefix.rstrip("/")
        if prefix and (bindery_path == prefix or bindery_path.startswith(prefix + "/")):
            # Most specific prefix wins; equal prefixes go to the row's own format.
            matches.append((len(prefix), root_fmt == fmt, root_fmt))
    return map_path(bindery_path, max(matches)[2] if matches else fmt)


def _unit_files(item: dict) -> list[Path]:
    fmt = str(item.get("format") or "ebook")
    unit = PurePosixPath(str(item.get("rootPath") or "")) / str(item.get("relPath") or "")
    local = Path(_local_path(str(unit), fmt))
    extensions = AUDIO_EXTENSIONS if fmt == "audiobook" else EBOOK_EXTENSIONS
    if local.is_file():
        siblings = [local.parent / name for name in item.get("members") or []]
        return [f for f in siblings if f.is_file()] or [local]
    if local.is_dir():
        return sorted(f for f in local.rglob("*") if f.is_file() and f.suffix.lower() in extensions)
    return []


def check_item(item: dict, library: Library, client: BinderyClient | None, ) -> dict:
    """Run every check on one unmatched row and decide its verdict."""
    evidence: list[dict] = []
    files = _unit_files(item)
    if not files:
        unit = PurePosixPath(str(item.get("rootPath") or "")) / str(item.get("relPath") or "")
        return {"verdict": "UNSURE", "reason": "BookGuard can't see these files from its own mounts.",
                "evidence": [{"check": "Files", "result": "warn",
                              "detail": f"Bindery's {unit} → {_local_path(str(unit), str(item.get('format') or 'ebook'))} "
                                        "does not exist inside BookGuard."}]}
    audio = str(item.get("format")) == "audiobook"
    facts = _audio_facts(files, evidence) if audio else _ebook_facts(files[0], evidence)
    if facts["junk"]:
        return {"verdict": "JUNK", "reason": facts["junk"], "evidence": evidence}

    identities = list(facts["identities"])
    for title, author in name_identities(files[0].name):
        identities.append(("File name", title, author))
    folder_author = str(item.get("authorFolder") or "")
    folder_title = _folder_title(item, folder_author)
    if folder_title:
        identities.append(("Folder names", folder_title, folder_author))
    for isbn in facts["isbns"][:3]:
        try:
            found = client.lookup_isbn(isbn) if client else None
        except BinderyClientError:
            found = None
        if found and found.get("title"):
            author = str((found.get("author") or {}).get("authorName") or "")
            identities.append(("ISBN lookup", found["title"], author))
            _check(evidence, "ISBN lookup", "pass", f"ISBN {isbn} is “{found['title']}” by {author or 'unknown'}.")
    for source, title, author in identities:
        if source not in {"File name", "ISBN lookup"}:  # both readings of a name would only confuse
            _check(evidence, source, "info", f"“{title}” by {author or 'unknown author'}")
    if name_identities(files[0].name):
        _check(evidence, "File name", "info", files[0].name)

    best = _best_identity(identities, facts["front"])
    if best and folder_author and _surname(folder_author) != best["surname"]:
        _check(evidence, "Folder", "warn", f"It sits in the “{folder_author}” folder, but the file says "
               f"{best['author'] or 'another author'}.")
    if best and "Title page" in best["sources"]:
        _check(evidence, "Title page", "pass", f"The first pages name “{best['title']}” and its author.")
    if not best:
        return {"verdict": "UNSURE", "reason": "It reads as a real book, but no two sources agree on what it is.",
                "evidence": evidence}

    if audio:
        # The shared analyser treats every nonempty credit as informative. Here
        # uploader placeholders ("Author's", "Unknown") already mean no author;
        # keep that meaning when checking the complete set as well.
        identity_probes = []
        for probe in facts["audio_probes"]:
            _, credit = audio_work_identity(probe)
            identity_probes.append({**probe, "author": credit if _surname(credit) else "",
                                    "album_artist": "", "artist": "", "composer": ""})
        agreement = analyze_audio_identity_set(best["title"], best["author"], identity_probes)
        conflicting = (
            agreement["hasEmbeddedContradiction"]
            or agreement["authorMatchCount"] < agreement["informativeAuthorCount"]
        )
        _check(evidence, "Whole recording identity", "warn" if conflicting else "pass",
               f"Checked identity across all {len(facts['audio_probes'])} audio files.")
        if conflicting:
            return {"verdict": "UNSURE", "evidence": evidence, "reason": (
                "The audio files name conflicting works or authors. A matching first track "
                "does not prove the whole folder belongs to one book; review it before attaching.")}

    title, author, sources = best["title"], best["author"], " + ".join(sorted(best["sources"]))
    folder_surname = _surname(folder_author)
    if audio and folder_surname and best["surname"] != folder_surname and best["named_by"] == {"Audio tags"}:
        # Only one source names this author and the folder names another: often the narrator.
        named_by = next(iter(best["named_by"]))
        return {"verdict": "UNSURE", "evidence": evidence, "reason": (
            f"The {named_by.lower()} name {author} as the author of “{title}”, but it sits in the “{folder_author}” "
            f"folder and nothing else names the author. {author} may be the narrator; check it yourself.")}
    book, choices = None, []
    for candidate in best["titles"]:  # the best reading first, then the others that agree with it
        book, found = library.find(candidate, author)
        choices = choices or found
        if book:
            break
    series = None if book else library.series_book(title, author)
    if series is not None:
        if series:
            book, choices = series, []
        else:
            return {"verdict": "UNSURE", "evidence": evidence, "reason": (
                f"“{title}” is a series name and number, not a book title, and BookGuard can't tell which of "
                f"{author}'s books it is.")}
    if book is None and choices:
        names = ", ".join(f"“{b['title']}”" for b in choices[:4])
        if len({_norm(b["title"]) for b in choices}) == 1:
            reason = (f"It is “{title}” by {author} ({sources}), but your library has that book more than once "
                      f"({names}), so BookGuard won't pick one. Remove the extra entry in Bindery, then check again.")
        else:
            reason = (f"It is “{title}” by {author} ({sources}), but that fits more than one book in your library "
                      f"({names}), so BookGuard won't pick one.")
        return {"verdict": "UNSURE", "reason": reason, "evidence": evidence}
    if book is None:
        related = {int(b["id"]): b for key in _title_keys(title) for b in library.by_title.get(key, [])
                   if _surname(b["author"]) == _surname(author) and ebook_title_conflict(b["title"], title)}
        if related:
            return {"verdict": "UNSURE", "evidence": evidence, "reason": (
                f"The file identifies “{title}”, but the related catalogue entry has a different "
                "part or volume designation. Check whether it names a work division or a series position.")}
        # The folder's author has a book by this title: likely a pen name ("Richard Bachman")
        # or a narrator in the author tag. A person should decide.
        alias = next((b for b in library.same_title(title)
                      if folder_author and _surname(b["author"]) == _surname(folder_author)), None)
        if alias:
            return {"verdict": "UNSURE", "evidence": evidence, "reason": (
                f"The file says “{title}” by {author or 'an unknown author'}, but your library has "
                f"“{alias['title']}” by {alias['author']}. The name may be a pen name or a narrator; check it yourself.")}
        reason = f"It is “{title}” by {author or 'an unknown author'} ({sources}), which is not in your library."
        return {"verdict": "NOT_IN_LIBRARY", "reason": reason, "evidence": evidence}

    found = {"bookId": int(book["id"]), "bookTitle": book["title"]}
    language = facts.get("language") or ""
    if language and _other_language(language, book, identities):
        return {"verdict": "OTHER_LANGUAGE", "evidence": evidence, **found, "reason": (
            f"It is a {language} edition of “{book['title']}” by {book['author']} ({sources}). "
            "It is not the same book file as your library's edition, so BookGuard won't attach it.")}

    # A library book: an exact copy of a file it already has is junk, any other copy is extra.
    kind = "audiobook" if audio else "ebook"
    try:
        existing = bindery_files_for_book(int(book["id"]), kind)
    except Exception:
        existing = []
    if existing and not audio:
        mine = sha256_file(files[0])
        for other in existing:
            local = Path(_local_path(str(other["stored_path"]), "ebook"))
            if local.is_file() and local.stat().st_size == files[0].stat().st_size and sha256_file(local) == mine:
                _check(evidence, "Exact copy", "fail", f"Identical to “{book['title']}”’s ebook already in the library.")
                return {"verdict": "JUNK", "reason": f"It is an exact copy of the ebook “{book['title']}” already has.",
                        "evidence": evidence}
    reason = f"It is “{book['title']}” by {book['author']} ({sources})."
    if existing:
        return {"verdict": "DUPLICATE", "evidence": evidence, **found, "reason": (
            f"{reason} That book already has an {kind}, so this is an extra copy. Attaching it would add a second one.")}
    return {"verdict": "BELONGS", "reason": reason, "evidence": evidence, **found}


def _folder_title(item: dict, folder_author: str) -> str:
    """The book's folder name, skipping folders like "eBook", "m4b", "US" or "Edition 1"."""
    parts = list(PurePosixPath(str(item.get("relPath") or "")).parts)
    if str(item.get("kind")) != "folder":
        parts = parts[:-1]  # a file row is "Author/Title/file"
    parts = parts[1:] if folder_author and parts and parts[0] == folder_author else parts
    for name in reversed(parts):
        name = re.sub(r"\(\d{4}\)", "", name).strip()
        if not name or GENERIC_FOLDER.match(name):
            continue
        if folder_author:  # "Jane Mendelsohn-American Music", "Never and Forever - Cressida Cowell", but
            # never "Autobiography of Mark Twain": the author must be set off by a dash or comma
            author = re.escape(folder_author)
            name = re.sub(rf"^{author}\s*[-–,]\s*", "", name, flags=re.IGNORECASE) or name
            name = re.sub(rf"\s*[-–]\s*{author}$", "", name, flags=re.IGNORECASE) or name
        return name
    return ""


SOURCE_RANK = {"ISBN lookup": 3, "Embedded metadata": 2, "Audio tags": 2, "Folder names": 1, "File name": 0}


def _source_title_conflict(source: str, title: str, other_source: str, other_title: str) -> bool:
    """Release names may annotate a named work with its series book number.

    Only name corroboration can ignore a trailing '(Book N)' label. Original
    explicit number conflicts remain blocking, and catalogue lookup still uses
    the original titles and work-division guard.
    """
    title = canonical_title_designations(title)
    other_title = canonical_title_designations(other_title)
    if title_number_conflict(title, other_title):
        return True
    book_number = r"\b(?:book|bk)\.?\s*(\d+)\s*[)\]]?\s*$"
    left = re.search(book_number, title, re.IGNORECASE)
    right = re.search(book_number, other_title, re.IGNORECASE)
    if left and right and int(left.group(1)) != int(right.group(1)):
        return True

    def reading(label: str, value: str) -> str:
        if label in NAME_SOURCES:
            return re.sub(r"\s*\((?:book|bk)\.?\s*\d+\)\s*$", "", value, flags=re.IGNORECASE)
        return value

    return ebook_title_conflict(reading(source, title), reading(other_source, other_title))


def _best_identity(identities: list[tuple[str, str, str]], front: str) -> dict | None:
    """The identity most sources agree on, if it is proven.

    A source supports a candidate when one of their title readings is shared and it names
    the same author or none (tags often lack one). Names and the title page only support an
    identity the file itself gives (embedded metadata, audio tags or an ISBN lookup)."""
    read = [(source, title, author, _title_keys(title, numbered=True), _surname(author))
            for source, title, author in identities]
    read = [r for r in read if r[3]]
    padded = f" {front} " if front else ""
    best = None
    for source, title, author, keys, surname in read:
        if not surname:
            continue
        supporting = [(s, t, k, n) for s, t, _, k, n in read
                      if k & keys and n in ("", surname) and not _source_title_conflict(source, title, s, t)]
        support = {s for s, _, _, _ in supporting}
        if padded and surname in padded.split() and any(f" {key} " in padded for key in keys if len(key) >= 4):
            support.add("Title page")
        if len(support) < 2 or not support - NAME_SOURCES - {"Title page"}:
            continue
        rank = (len(support), SOURCE_RANK.get(source, 0))
        if best is None or rank > best["rank"]:
            named_by = {s for s, _, _, n in supporting if n == surname}
            titles = [title] + [t for _, t, _, _ in supporting if t != title]
            best = {"title": title, "author": author, "surname": surname, "sources": support, "rank": rank,
                    "named_by": named_by, "titles": list(dict.fromkeys(titles))}
    return best


def _other_language(language: str, book: dict, identities: list[tuple[str, str, str]]) -> bool:
    """True when the text is in another language than the library's edition."""
    book_language = str(book.get("language") or "").strip().lower()
    known = {code for codes in LANGUAGE_CODES.values() for code in codes}
    if book_language in known:
        return book_language not in LANGUAGE_CODES.get(language, set())
    # Bindery doesn't say: a non-English text whose own title isn't the book's is a translation.
    own = [title for source, title, _ in identities if source == "Embedded metadata"]
    return language != "English" and bool(own) and not any(_title_keys(t) & _title_keys(book["title"]) for t in own)


# ---- running it ---------------------------------------------------------------

CHECK_VERSION = 8  # raise when the checks change, so stored verdicts are worked out again


def _signature(item: dict) -> str:
    return (f"v{CHECK_VERSION}|{item.get('rootPath')}/{item.get('relPath')}"
            f"|{item.get('fileCount')}|{item.get('sizeBytes')}")


def _all_rows(client: BinderyClient) -> list[dict]:
    items, offset = [], 0
    while True:
        page = client.list_unmatched(offset=offset)
        batch = page.get("items") or []
        items.extend(batch)
        offset += len(batch)
        if not batch or offset >= int(page.get("total") or 0):
            return items


def _find_row(client: BinderyClient, row_id: int) -> dict | None:
    return next((item for item in _all_rows(client) if int(item["id"]) == row_id), None)


def check_unmatched(client: BinderyClient | None = None, *, force: bool = False) -> dict:
    """Check every pending unmatched row once (again only when its files change)."""
    init_unmatched_db()
    client = client or BinderyClient(api_key=resolve_bindery_api_key(), timeout=30)
    library = Library(library_books())
    with local_conn() as conn:
        known = {int(r["row_id"]): r["signature"] for r in conn.execute("SELECT row_id, signature FROM unmatched_checks")}
    items = _all_rows(client)
    _STATUS.update(total=len(items), checked=0)
    seen = set()
    for item in items:
        row_id, signature = int(item["id"]), _signature(item)
        seen.add(row_id)
        if not force and known.get(row_id) == signature:
            _STATUS["checked"] += 1
            continue
        try:
            outcome = check_item(item, library, client)
        except Exception as exc:  # one odd file must not stop the rest
            outcome = {"verdict": "UNSURE", "reason": f"Checking it failed: {exc}", "evidence": []}
        _save(row_id, signature, item, outcome)
        _STATUS["checked"] += 1
    with local_conn() as conn:  # rows Bindery no longer lists were adopted, ignored or removed
        for row_id in set(known) - seen:
            conn.execute("DELETE FROM unmatched_checks WHERE row_id=?", (row_id,))
        conn.commit()
    return {"checked": len(items)}


def start_check(*, force: bool = False) -> bool:
    if not _LOCK.acquire(blocking=False):
        return False

    def run() -> None:
        _STATUS.update(running=True, error="")
        try:
            check_unmatched(force=force)
        except Exception as exc:
            _STATUS["error"] = str(exc)[:500]
        finally:
            _STATUS.update(running=False, finishedAt=datetime.now(timezone.utc).isoformat())
            _LOCK.release()

    threading.Thread(target=run, name="bookguard-unmatched", daemon=True).start()
    return True


def check_status() -> dict:
    return dict(_STATUS)


def attach(row_id: int) -> str:
    """Adopt one proven row to its book through Bindery's own adopt (Bindery can undo it)."""
    if not settings.allow_actions:
        raise ActionError("Actions are disabled. Turn them on in Settings to attach files.")
    init_unmatched_db()
    with local_conn() as conn:
        row = conn.execute("SELECT * FROM unmatched_checks WHERE row_id=?", (int(row_id),)).fetchone()
    if not row or row["verdict"] != "BELONGS" or not row["book_id"]:
        raise ActionError("Only files BookGuard proved belong to a book can be attached.")
    client = BinderyClient(api_key=resolve_bindery_api_key(), timeout=30)
    # Re-check right before changing Bindery: the row must still be listed with the
    # same files, and the full proof must name the same book again.
    item = _find_row(client, int(row_id))
    if item is None:
        with local_conn() as conn:
            conn.execute("DELETE FROM unmatched_checks WHERE row_id=?", (int(row_id),))
            conn.commit()
        raise ActionError("Bindery no longer lists these files as unmatched. Nothing was changed.")
    outcome = check_item(item, Library(library_books()), client)
    _save(int(row_id), _signature(item), item, outcome)
    if _signature(item) != row["signature"] or outcome["verdict"] != "BELONGS" or outcome.get("bookId") != row["book_id"]:
        raise ActionError("The files, catalogue, or identity rules changed since the last check, so nothing was attached. "
                          "The page shows the new result.")
    try:
        client.adopt_unmatched(int(row_id), int(row["book_id"]))
    except BinderyClientError as exc:
        raise ActionError(f"Bindery refused to attach it: {exc}") from exc
    with local_conn() as conn:
        conn.execute("DELETE FROM unmatched_checks WHERE row_id=?", (int(row_id),))
        conn.commit()
    return f"Attached to “{row['book_title']}”. Bindery can undo this on its Import page (Adopted)."

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
* BELONGS: at least two independent sources point at the same Bindery book.
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
from .pdf_probe import probe_pdf
from .scanner import map_path

VERDICTS = {
    "JUNK": "Junk",
    "NOT_IN_LIBRARY": "Not in your library",
    "BELONGS": "Belongs to a book",
    "UNSURE": "Unsure",
}
EBOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw", ".azw3", ".txt", ".rtf", ".cbz"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".aac", ".flac", ".ogg", ".opus", ".wma", ".wav"}
MIN_BOOK_WORDS = 300  # fewer readable words than this is not a book
SAMPLE_MAX_WORDS = 25000  # a "free sample" notice in a text this short means a sample
MIN_AUDIO_SECONDS = 5 * 60
MAX_AUDIO_FILES = 60
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
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"^(the|a|an) ", "", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def _surname(author: str) -> str:
    first = re.split(r";|&| and |,(?=\s*\w+\s)", str(author or ""))[0]
    words = _norm(first).split()
    return words[-1] if words else ""


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


_PART = re.compile(r"[\s._-]*(\(?\d+\s*(-|of|/)\s*\d+\)?|part\s*\d+|cd\s*\d+|disc\s*\d+|\d{1,3})$", re.IGNORECASE)


def name_identities(name: str) -> list[tuple[str, str]]:
    """(title, author) readings of an 'X - Y' file name, both orders: releases use either."""
    stem = _PART.sub("", PurePosixPath(name).stem).strip()
    parts = [p.strip() for p in re.split(r"\s+-\s+", stem) if p.strip()]
    if len(parts) != 2:
        return []
    return [(parts[1], parts[0]), (parts[0], parts[1])]


NAME_SOURCES = {"File name", "Folder names"}  # one uploader often names both: never proof on their own


# ---- the library -------------------------------------------------------------

def library_books() -> list[dict]:
    with bindery_conn() as conn:
        rows = conn.execute(
            "SELECT b.id, b.title, a.name AS author FROM books b JOIN authors a ON a.id = b.author_id"
        ).fetchall()
    return [dict(row) for row in rows]


class Library:
    def __init__(self, books: list[dict]):
        self.books = {int(b["id"]): b for b in books}
        self.by_title: dict[str, list[dict]] = defaultdict(list)
        self.surnames: set[str] = set()
        for book in books:
            self.by_title[_norm(book["title"])].append(book)
            self.surnames.add(_surname(book["author"]))

    def match(self, title: str, author: str) -> dict | None:
        surname = _surname(author)
        for book in self.by_title.get(_norm(title), []):
            if surname and _surname(book["author"]) == surname:
                return book
        return None


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
    metadata = identity.metadata or {}
    if metadata.get("title"):
        facts["identities"].append(("Embedded metadata", metadata.get("title"), metadata.get("author") or ""))
    facts["front"] = _norm((identity.front_text or identity.text or "")[:6000])
    facts["isbns"] = file_isbns(identity.identifiers)
    return facts


def _audio_facts(files: list[Path], evidence: list) -> dict:
    facts: dict[str, Any] = {"junk": "", "identities": [], "front": "", "isbns": []}
    probes = [cached_or_probe_audio_file(str(f))[0] for f in files[:MAX_AUDIO_FILES]]
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
    first = probes[0]
    title = first.get("album") or first.get("title") or ""
    author = first.get("author") or first.get("album_artist") or first.get("artist") or ""
    if title:
        facts["identities"].append(("Audio tags", title, author))
    return facts


# ---- verdict -----------------------------------------------------------------

def _unit_files(item: dict) -> list[Path]:
    fmt = str(item.get("format") or "ebook")
    unit = PurePosixPath(str(item.get("rootPath") or "")) / str(item.get("relPath") or "")
    local = Path(map_path(str(unit), fmt))
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
        return {"verdict": "UNSURE", "reason": "BookGuard can't see these files from its own mounts.",
                "evidence": [{"check": "Files", "result": "warn", "detail": "Not found under BookGuard's library folders."}]}
    audio = str(item.get("format")) == "audiobook"
    facts = _audio_facts(files, evidence) if audio else _ebook_facts(files[0], evidence)
    if facts["junk"]:
        return {"verdict": "JUNK", "reason": facts["junk"], "evidence": evidence}

    identities = list(facts["identities"])
    for title, author in name_identities(files[0].name):
        identities.append(("File name", title, author))
    folder_author = str(item.get("authorFolder") or "")
    rel_parts = PurePosixPath(str(item.get("relPath") or "")).parts
    # A folder row is "Author/Title"; a file row is "Author/Title/file".
    folder_title = rel_parts[-1] if str(item.get("kind")) == "folder" else (rel_parts[-2] if len(rel_parts) >= 3 else "")
    if folder_author and len(rel_parts) >= 2 and folder_title and folder_title != folder_author:
        identities.append(("Folder names", re.sub(r"\(\d{4}\)", "", folder_title).strip(), folder_author))
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
        if source != "File name":  # both readings of a name would only confuse
            _check(evidence, source, "info", f"“{title}” by {author or 'unknown author'}")
    if name_identities(files[0].name):
        _check(evidence, "File name", "info", files[0].name)

    # Group sources that name the same book (title + author surname).
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    shown: dict[tuple[str, str], tuple[str, str]] = {}
    for source, title, author in identities:
        key = (_norm(title), _surname(author))
        if not key[0]:
            continue
        groups[key].append(source)
        shown.setdefault(key, (title, author))
    front = facts["front"]
    for key in groups:
        if front and key[0] and key[0] in front and (not key[1] or key[1] in front):
            groups[key].append("Title page")
    def proof(sources: list[str]) -> bool:
        # Names and the title page only support an identity the file itself gives
        # (embedded metadata, audio tags or an ISBN lookup); never proof on their own.
        distinct = set(sources)
        return len(distinct) >= 2 and bool(distinct - NAME_SOURCES - {"Title page"})

    proven = [kv for kv in groups.items() if proof(kv[1])]
    best = max(proven, key=lambda kv: len(set(kv[1])), default=None)
    if folder_author and best and best[0][1] and _surname(folder_author) != best[0][1]:
        _check(evidence, "Folder", "warn", f"It sits in the “{folder_author}” folder, but the file says "
               f"{shown[best[0]][1] or 'another author'}.")
    if best and "Title page" in best[1]:
        _check(evidence, "Title page", "pass", f"The first pages name “{shown[best[0]][0]}” and its author.")
    if not best:
        return {"verdict": "UNSURE", "reason": "It reads as a real book, but no two sources agree on what it is.",
                "evidence": evidence}

    title, author = shown[best[0]]
    sources = sorted(set(best[1]))
    book = library.match(title, author)
    if book is None:
        reason = f"It is “{title}” by {author or 'an unknown author'} ({' + '.join(sources)}), which is not in your library."
        return {"verdict": "NOT_IN_LIBRARY", "reason": reason, "evidence": evidence}

    # Belongs to a library book: an exact copy of a file it already has is junk.
    try:
        existing = bindery_files_for_book(int(book["id"]), "audiobook" if audio else "ebook")
    except Exception:
        existing = []
    if existing and not audio:
        mine = sha256_file(files[0])
        for other in existing:
            local = Path(map_path(str(other["stored_path"]), "ebook"))
            if local.is_file() and local.stat().st_size == files[0].stat().st_size and sha256_file(local) == mine:
                _check(evidence, "Exact copy", "fail", f"Identical to “{book['title']}”’s ebook already in the library.")
                return {"verdict": "JUNK", "reason": f"It is an exact copy of the ebook “{book['title']}” already has.",
                        "evidence": evidence}
    reason = f"It is “{book['title']}” by {book['author']} ({' + '.join(sources)})."
    if existing:
        reason += " That book already has a file in this format."
    return {"verdict": "BELONGS", "reason": reason, "bookId": int(book["id"]), "bookTitle": book["title"],
            "evidence": evidence}


# ---- running it ---------------------------------------------------------------

def _signature(item: dict) -> str:
    return f"{item.get('rootPath')}/{item.get('relPath')}|{item.get('fileCount')}|{item.get('sizeBytes')}"


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
        raise ActionError("The files changed since the last check, so nothing was attached. "
                          "The page shows the new result.")
    try:
        client.adopt_unmatched(int(row_id), int(row["book_id"]))
    except BinderyClientError as exc:
        raise ActionError(f"Bindery refused to attach it: {exc}") from exc
    with local_conn() as conn:
        conn.execute("DELETE FROM unmatched_checks WHERE row_id=?", (int(row_id),))
        conn.commit()
    return f"Attached to “{row['book_title']}”. Bindery can undo this on its Import page (Adopted)."

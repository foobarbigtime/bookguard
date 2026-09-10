from __future__ import annotations

from pathlib import Path
import html
import re
import zipfile
import xml.etree.ElementTree as ET

from . import verifier as legacy
from .config import settings
from .metadata import ebook_metadata
from .matcher import author_match_strict, author_mentioned_in_text, meaningful_words, normalize

# v0.4.8 deliberately invalidates v0.4.7 cached verdicts.
legacy.VERIFIER_VERSION = "2"

FRONT_TEXT_CHARS = 160_000
PROXIMITY_CHARS = 3_000
MIN_USEFUL_TEXT = 500


def _strip_html_bytes(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _epub_identity(path: str) -> tuple[dict, str, list[str], str]:
    metadata = {"title": "", "author": "", "source": "epub"}
    identifiers: list[str] = []
    max_chars = settings.verification_max_text_chars
    text_parts: list[str] = []
    front_parts: list[str] = []

    with zipfile.ZipFile(path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = ""
        for elem in container.iter():
            if elem.tag.endswith("rootfile"):
                rootfile = elem.attrib.get("full-path", "")
                if rootfile:
                    break
        if not rootfile:
            raise ValueError("EPUB package file not found")

        package = ET.fromstring(zf.read(rootfile))
        creators: list[str] = []
        manifest: dict[str, str] = {}
        spine_ids: list[str] = []
        package_dir = str(Path(rootfile).parent)

        for elem in package.iter():
            local = elem.tag.rsplit("}", 1)[-1].lower()
            value = (elem.text or "").strip()
            if local == "title" and value and not metadata["title"]:
                metadata["title"] = value
            elif local == "creator" and value:
                creators.append(value)
            elif local == "identifier" and value:
                identifiers.append(value)
            elif local == "item":
                item_id = elem.attrib.get("id", "")
                href = elem.attrib.get("href", "")
                media_type = elem.attrib.get("media-type", "")
                if item_id and href and media_type in {"application/xhtml+xml", "text/html"}:
                    manifest[item_id] = href
            elif local == "itemref":
                idref = elem.attrib.get("idref", "")
                if idref:
                    spine_ids.append(idref)

        metadata["author"] = "; ".join(creators)

        ordered_names: list[str] = []
        for idref in spine_ids:
            href = manifest.get(idref)
            if not href:
                continue
            name = str(Path(package_dir) / href) if package_dir not in {"", "."} else href
            ordered_names.append(name.replace("\\", "/"))

        # Broken EPUBs sometimes have no usable spine. Fall back to archive order.
        if not ordered_names:
            ordered_names = [
                name for name in zf.namelist()
                if name.lower().endswith((".xhtml", ".html", ".htm"))
            ]

        total = 0
        front_total = 0
        for name in ordered_names:
            if total >= max_chars:
                break
            try:
                part = _strip_html_bytes(zf.read(name))
            except Exception:
                continue
            if not part:
                continue
            text_parts.append(part)
            total += len(part)
            if front_total < FRONT_TEXT_CHARS:
                remaining = FRONT_TEXT_CHARS - front_total
                front_parts.append(part[:remaining])
                front_total += min(len(part), remaining)

    text = " ".join(text_parts)[:max_chars]
    front = " ".join(front_parts)[:FRONT_TEXT_CHARS]
    return metadata, text, identifiers[:25], front


def _pdf_identity(path: str) -> tuple[dict, str, list[str], str]:
    from pypdf import PdfReader

    metadata = ebook_metadata(path)
    reader = PdfReader(path)
    parts: list[str] = []
    front_parts: list[str] = []
    max_chars = settings.verification_max_text_chars
    page_limit = min(len(reader.pages), settings.verification_pdf_pages)
    for index, page in enumerate(reader.pages[:page_limit]):
        if sum(len(part) for part in parts) >= max_chars:
            break
        try:
            part = page.extract_text() or ""
        except Exception:
            continue
        parts.append(part)
        if index < 6:
            front_parts.append(part)
    text = " ".join(parts)[:max_chars]
    front = " ".join(front_parts)[:FRONT_TEXT_CHARS]
    return metadata, text, [], front


def _plain_identity(path: str) -> tuple[dict, str, list[str], str]:
    metadata = ebook_metadata(path)
    raw = Path(path).read_bytes()[: settings.verification_max_text_chars * 2]
    text = raw.decode("utf-8", errors="replace")
    if "\ufffd" in text[:4096]:
        text = raw.decode("cp1252", errors="replace")
    if Path(path).suffix.lower() == ".rtf":
        text = re.sub(r"\\[a-z]+-?\d* ?", " ", text, flags=re.I)
        text = text.replace("{", " ").replace("}", " ")
    text = re.sub(r"\s+", " ", text)[: settings.verification_max_text_chars]
    return metadata, text, [], text[:FRONT_TEXT_CHARS]


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


def _identity_signal(title: str, author: str, full_text: str, front_text: str) -> dict:
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
    ew = meaningful_words(expected)
    ow = meaningful_words(observed)
    return bool(ew) and ew == ow


def _classify_identity(result: dict, metadata: dict, text: str, identifiers: list[str], source: str, notes: list[str], front_text: str = "") -> tuple[str, int, dict]:
    expected_title = str(result.get("title") or "")
    expected_author = str(result.get("author") or "")
    embedded_title = str(metadata.get("title") or "")
    embedded_author = str(metadata.get("author") or "")
    front_text = front_text or text[:FRONT_TEXT_CHARS]

    expected = _identity_signal(expected_title, expected_author, text, front_text)
    embedded = _identity_signal(embedded_title, embedded_author, text, front_text)

    metadata_title_match = _title_identity_match(expected_title, embedded_title)
    metadata_author_match = author_match_strict(expected_author, embedded_author)
    metadata_matches_expected = metadata_title_match and metadata_author_match

    evidence = {
        "expected": {"title": expected_title, "author": expected_author},
        "embedded": {"title": embedded_title, "author": embedded_author, "identifiers": identifiers},
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

    if metadata_matches_expected and expected["strong_identity"]:
        evidence["explanation"] = "Embedded metadata matches the expected book and front-of-book content independently supports that identity."
        return "VERIFIED_CORRECT", 99, evidence

    if not metadata_matches_expected and expected["strong_identity"] and not embedded["strong_identity"]:
        evidence["explanation"] = "Front-of-book content strongly identifies the expected Bindery book, while the conflicting embedded identity is not strongly supported there."
        return "METADATA_ERROR", 97, evidence

    if (
        not metadata_matches_expected
        and embedded_title
        and embedded_author
        and embedded["strong_identity"]
        and not expected["title_found"]
        and not expected["author_found"]
    ):
        evidence["explanation"] = "Front-of-book content strongly supports the conflicting embedded title and author, while neither expected identity field was found anywhere in extracted content."
        return "WRONG_CONTENT", 99, evidence

    if metadata_matches_expected and (expected["title_found"] or expected["author_found"]):
        evidence["explanation"] = "Metadata matches the expected book, but content evidence is not positioned strongly enough for a 99% verdict."
        return "VERIFIED_CORRECT", 90, evidence

    if expected["title_found"] and expected["author_found"] and not metadata_matches_expected:
        evidence["explanation"] = "Expected title and author occur in the book, but their location/proximity is not strong enough to distinguish true identity from backmatter, series lists, or advertisements."
        return "INSUFFICIENT_EVIDENCE", 70, evidence

    evidence["explanation"] = "BookGuard could not obtain position-aware identity evidence strong enough for an automatic verdict."
    return "INSUFFICIENT_EVIDENCE", 40, evidence


def verify_result(result: dict, force: bool = False) -> dict:
    if not settings.verification_enabled:
        raise RuntimeError("Content verification is disabled in Settings.")

    target = legacy._resolve_target(result.get("local_path") or "")
    fingerprint = legacy._file_fingerprint(target)
    if not force:
        cached = legacy.verification_for_result(result)
        if cached:
            return cached

    if result.get("format") != "ebook":
        evidence = {
            "expected": {"title": result.get("title", ""), "author": result.get("author", "")},
            "embedded": {}, "content": {}, "metadata_matches_expected": False,
            "notes": ["Audiobook content verification is not implemented yet; existing tag-based scanning remains in use."],
            "explanation": "This verifier currently establishes book identity from ebook content only.",
        }
        return legacy._save_verification(result, target, fingerprint, "INSUFFICIENT_EVIDENCE", 0, "unsupported-audiobook", evidence)

    path = Path(target)
    if not path.is_file():
        evidence = {
            "expected": {"title": result.get("title", ""), "author": result.get("author", "")},
            "embedded": {}, "content": {}, "metadata_matches_expected": False,
            "notes": ["The tracked ebook target could not be opened as a file."],
            "explanation": "No readable ebook file was available for content verification.",
        }
        return legacy._save_verification(result, target, fingerprint, "INSUFFICIENT_EVIDENCE", 0, "missing", evidence)

    suffix = path.suffix.lower()
    metadata = ebook_metadata(str(path))
    text = ""
    front_text = ""
    identifiers: list[str] = []
    source = "native"
    notes: list[str] = []

    try:
        if suffix == ".epub":
            metadata, text, identifiers, front_text = _epub_identity(str(path))
            source = "native-epub"
        elif suffix == ".pdf":
            metadata, text, identifiers, front_text = _pdf_identity(str(path))
            source = "native-pdf"
        elif suffix in {".txt", ".rtf"}:
            metadata, text, identifiers, front_text = _plain_identity(str(path))
            source = f"native-{suffix.lstrip('.')}"
    except Exception as exc:
        notes.append(f"Native extraction error: {str(exc)[:300]}")

    # Tika remains optional. Use it only when native extraction is genuinely weak.
    if len(text.strip()) < MIN_USEFUL_TEXT and settings.verification_use_tika and settings.verification_tika_url:
        tika_text, tika_error = legacy._tika_text(str(path))
        if tika_text:
            text = tika_text
            front_text = tika_text[:FRONT_TEXT_CHARS]
            source = f"{source}+tika" if source != "native" else "tika"
            notes.append("Tika fallback supplied content because native extraction was insufficient.")
        elif tika_error:
            notes.append(tika_error)

    verdict, confidence, evidence = _classify_identity(result, metadata, text, identifiers, source, notes, front_text)
    return legacy._save_verification(result, str(path), fingerprint, verdict, confidence, source, evidence)


# Reuse the mature persistence/job/repair machinery, but make all of it call
# the v0.4.8 verifier. Setting the module global also makes cache signatures v2.
legacy.verify_result = verify_result

init_verification_db = legacy.init_verification_db
verification_for_result = legacy.verification_for_result
verification_summary = legacy.verification_summary
verification_job_status = legacy.verification_job_status
start_verification_job = legacy.start_verification_job
test_tika = legacy.test_tika
verified_repair_preview = legacy.verified_repair_preview
apply_verified_metadata_repair = legacy.apply_verified_metadata_repair

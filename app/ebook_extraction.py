from __future__ import annotations

from dataclasses import dataclass
import html
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from .config import settings
from .metadata import ebook_metadata
from .tika_client import extract_text as tika_text
from .verification_constants import FRONT_TEXT_CHARS, MIN_USEFUL_TEXT


@dataclass(frozen=True)
class ExtractedEbookIdentity:
    """Metadata and text evidence extracted from one ebook file."""

    metadata: dict
    text: str
    identifiers: list[str]
    front_text: str
    source: str
    notes: list[str]


def _strip_html_bytes(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def extract_epub_identity(path: str) -> tuple[dict, str, list[str], str]:
    metadata = {"title": "", "author": "", "source": "epub"}
    identifiers: list[str] = []
    max_chars = settings.verification_max_text_chars
    text_parts: list[str] = []
    front_parts: list[str] = []

    with zipfile.ZipFile(path) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = ""
        for element in container.iter():
            if element.tag.endswith("rootfile"):
                rootfile = element.attrib.get("full-path", "")
                if rootfile:
                    break
        if not rootfile:
            raise ValueError("EPUB package file not found")

        package = ET.fromstring(archive.read(rootfile))
        creators: list[str] = []
        manifest: dict[str, str] = {}
        spine_ids: list[str] = []
        package_dir = str(Path(rootfile).parent)

        for element in package.iter():
            local = element.tag.rsplit("}", 1)[-1].lower()
            value = (element.text or "").strip()
            if local == "title" and value and not metadata["title"]:
                metadata["title"] = value
            elif local == "creator" and value:
                creators.append(value)
            elif local == "identifier" and value:
                identifiers.append(value)
            elif local == "item":
                item_id = element.attrib.get("id", "")
                href = element.attrib.get("href", "")
                media_type = element.attrib.get("media-type", "")
                if item_id and href and media_type in {"application/xhtml+xml", "text/html"}:
                    manifest[item_id] = href
            elif local == "itemref":
                idref = element.attrib.get("idref", "")
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

        if not ordered_names:
            ordered_names = [
                name
                for name in archive.namelist()
                if name.lower().endswith((".xhtml", ".html", ".htm"))
            ]

        total = 0
        front_total = 0
        for name in ordered_names:
            if total >= max_chars:
                break
            try:
                part = _strip_html_bytes(archive.read(name))
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


def extract_pdf_identity(path: str) -> tuple[dict, str, list[str], str]:
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


def extract_plain_identity(path: str) -> tuple[dict, str, list[str], str]:
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


def extract_ebook_identity(path: str) -> ExtractedEbookIdentity:
    """Extract native ebook evidence and use Tika only when configured and needed."""
    suffix = Path(path).suffix.lower()
    metadata = ebook_metadata(path)
    text = ""
    front_text = ""
    identifiers: list[str] = []
    source = "native"
    notes: list[str] = []

    try:
        if suffix == ".epub":
            metadata, text, identifiers, front_text = extract_epub_identity(path)
            source = "native-epub"
        elif suffix == ".pdf":
            metadata, text, identifiers, front_text = extract_pdf_identity(path)
            source = "native-pdf"
        elif suffix in {".txt", ".rtf"}:
            metadata, text, identifiers, front_text = extract_plain_identity(path)
            source = f"native-{suffix.lstrip('.')}"
    except Exception as exc:
        notes.append(f"Native extraction error: {str(exc)[:300]}")

    if (
        len(text.strip()) < MIN_USEFUL_TEXT
        and settings.verification_use_tika
        and settings.verification_tika_url
    ):
        fallback_text, tika_error = tika_text(path)
        if fallback_text:
            text = fallback_text
            front_text = fallback_text[:FRONT_TEXT_CHARS]
            source = f"{source}+tika" if source != "native" else "tika"
            notes.append("Tika fallback supplied content because native extraction was insufficient.")
        elif tika_error:
            notes.append(tika_error)

    return ExtractedEbookIdentity(
        metadata=metadata,
        text=text,
        identifiers=identifiers,
        front_text=front_text,
        source=source,
        notes=notes,
    )

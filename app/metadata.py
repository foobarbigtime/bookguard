from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import struct
import subprocess
import zipfile
import xml.etree.ElementTree as ET

from .media_discovery import AUDIO_EXTENSIONS, representative_audio_files


def audio_files(path: str, limit: int) -> list[str]:
    """Return deterministic representative audiobook files for metadata sampling."""
    return representative_audio_files(path, limit)


def ffprobe_metadata(path: str) -> dict:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "format=duration:format_tags=artist,album_artist,author,composer,album,title,genre",
        "-of", "json", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        return {"probe_error": proc.stderr.strip()[:500], "path": path}
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"probe_error": "ffprobe returned invalid JSON", "path": path}
    fmt = payload.get("format") or {}
    tags = fmt.get("tags") or {}
    normalized = {str(k).lower(): v for k, v in tags.items()}
    return {
        "path": path,
        "artist": normalized.get("artist", ""),
        "album_artist": normalized.get("album_artist", ""),
        "author": normalized.get("author", ""),
        "composer": normalized.get("composer", ""),
        "album": normalized.get("album", ""),
        "title": normalized.get("title", ""),
        "genre": normalized.get("genre", ""),
        "duration": fmt.get("duration", ""),
    }


def _normalized_key(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def _most_common_display(values: list[str]) -> str:
    cleaned = [str(value).strip() for value in values if str(value or "").strip()]
    if not cleaned:
        return ""
    keyed = [_normalized_key(value) for value in cleaned]
    counts = Counter(keyed)
    key, _ = counts.most_common(1)[0]
    for value, normalized in zip(cleaned, keyed):
        if normalized == key:
            return value
    return cleaned[0]


def audio_metadata_summary(samples: list[dict]) -> dict:
    titles: list[str] = []
    authors: list[str] = []
    genres: list[str] = []
    for sample in samples:
        title = str(sample.get("album") or sample.get("title") or "").strip()
        author = str(
            sample.get("author")
            or sample.get("album_artist")
            or sample.get("artist")
            or sample.get("composer")
            or ""
        ).strip()
        genre = str(sample.get("genre") or "").strip()
        if title:
            titles.append(title)
        if author:
            authors.append(author)
        if genre:
            genres.append(genre)
    return {
        "detected_title": _most_common_display(titles),
        "detected_author": _most_common_display(authors),
        "detected_genre": _most_common_display(genres),
    }


def epub_metadata(path: str) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            container = ET.fromstring(zf.read("META-INF/container.xml"))
            rootfile = None
            for elem in container.iter():
                if elem.tag.endswith("rootfile"):
                    rootfile = elem.attrib.get("full-path")
                    if rootfile:
                        break
            if not rootfile:
                return {"error": "EPUB container does not declare an OPF package."}
            package = ET.fromstring(zf.read(rootfile))
            title = ""
            creators: list[str] = []
            for elem in package.iter():
                local = elem.tag.rsplit("}", 1)[-1].lower()
                if local == "title" and not title and elem.text:
                    title = elem.text.strip()
                elif local == "creator" and elem.text:
                    creators.append(elem.text.strip())
            return {"title": title, "author": "; ".join(creators), "source": "epub"}
    except Exception as exc:
        return {"error": str(exc)[:500], "source": "epub"}


def pdf_metadata(path: str) -> dict:
    try:
        from pypdf import PdfReader
        reader = PdfReader(path)
        md = reader.metadata or {}
        return {
            "title": str(md.get("/Title") or "").strip(),
            "author": str(md.get("/Author") or "").strip(),
            "source": "pdf",
        }
    except Exception as exc:
        return {"error": str(exc)[:500], "source": "pdf"}


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("MOBI field points outside the file")
    return struct.unpack(">I", data[offset:offset + 4])[0]


def _decode_mobi_text(raw: bytes, encoding_id: int) -> str:
    codec = "utf-8" if encoding_id == 65001 else "cp1252"
    return raw.decode(codec, errors="replace").replace("\x00", "").strip()


def mobi_metadata(path: str) -> dict:
    """Read title/author from MOBI/AZW/AZW3 PalmDB/EXTH metadata."""
    try:
        data = Path(path).read_bytes()
        if len(data) < 86:
            raise ValueError("File is too small to contain a MOBI header")
        record_count = struct.unpack(">H", data[76:78])[0]
        if record_count < 1:
            raise ValueError("MOBI file has no PalmDB records")
        record0_offset = _u32(data, 78)
        record0_end = len(data)
        if record_count > 1 and len(data) >= 90:
            record0_end = _u32(data, 86)
        if not (0 <= record0_offset < record0_end <= len(data)):
            raise ValueError("Invalid MOBI record offsets")
        record = data[record0_offset:record0_end]

        mobi_start = record.find(b"MOBI", 8, min(len(record), 128))
        if mobi_start < 0:
            raise ValueError("MOBI header signature was not found")
        if mobi_start + 0x74 > len(record):
            raise ValueError("Truncated MOBI header")

        header_length = _u32(record, mobi_start + 4)
        encoding_id = _u32(record, mobi_start + 0x0C)
        title = ""
        authors: list[str] = []

        # MOBI Full Name offset/length are normally relative to record 0.
        full_name_offset = _u32(record, mobi_start + 0x44)
        full_name_length = _u32(record, mobi_start + 0x48)
        for candidate_offset in (full_name_offset, mobi_start + full_name_offset):
            if full_name_length and 0 <= candidate_offset < len(record):
                end = candidate_offset + full_name_length
                if end <= len(record):
                    candidate = _decode_mobi_text(record[candidate_offset:end], encoding_id)
                    if candidate:
                        title = candidate
                        break

        exth_start = mobi_start + header_length
        if exth_start + 12 <= len(record) and record[exth_start:exth_start + 4] == b"EXTH":
            exth_length = _u32(record, exth_start + 4)
            exth_count = _u32(record, exth_start + 8)
            pos = exth_start + 12
            exth_end = min(len(record), exth_start + exth_length)
            for _ in range(exth_count):
                if pos + 8 > exth_end:
                    break
                record_type = _u32(record, pos)
                record_length = _u32(record, pos + 4)
                if record_length < 8 or pos + record_length > exth_end:
                    break
                value = _decode_mobi_text(record[pos + 8:pos + record_length], encoding_id)
                if record_type == 100 and value:
                    authors.append(value)
                elif record_type == 503 and value:
                    title = value
                pos += record_length

        # Preserve order while removing duplicate author records.
        deduped_authors: list[str] = []
        seen: set[str] = set()
        for author in authors:
            key = author.casefold()
            if key not in seen:
                seen.add(key)
                deduped_authors.append(author)

        return {
            "title": title,
            "author": "; ".join(deduped_authors),
            "source": "mobi",
        }
    except Exception as exc:
        return {"error": str(exc)[:500], "source": "mobi"}


def cbz_metadata(path: str) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            names = {name.casefold(): name for name in zf.namelist()}
            comic_info_name = names.get("comicinfo.xml")
            if not comic_info_name:
                return {"title": "", "author": "", "source": "cbz"}
            root = ET.fromstring(zf.read(comic_info_name))
            values = {child.tag.casefold(): (child.text or "").strip() for child in root}
            author = values.get("writer", "") or values.get("creator", "")
            return {
                "title": values.get("title", ""),
                "author": author,
                "source": "cbz",
            }
    except Exception as exc:
        return {"error": str(exc)[:500], "source": "cbz"}


def rtf_metadata(path: str) -> dict:
    try:
        text = Path(path).read_bytes()[:262144].decode("latin-1", errors="replace")
        title_match = re.search(r"\\title\s+([^{}\\]+)", text, flags=re.IGNORECASE)
        author_match = re.search(r"\\author\s+([^{}\\]+)", text, flags=re.IGNORECASE)
        return {
            "title": title_match.group(1).strip() if title_match else "",
            "author": author_match.group(1).strip() if author_match else "",
            "source": "rtf",
        }
    except Exception as exc:
        return {"error": str(exc)[:500], "source": "rtf"}


def txt_metadata(path: str) -> dict:
    """Use only explicit Title:/Author: headers; never trust the filename as content evidence."""
    try:
        raw = Path(path).read_bytes()[:131072]
        text = raw.decode("utf-8", errors="replace")
        if "\ufffd" in text[:4096]:
            text = raw.decode("cp1252", errors="replace")
        title = ""
        author = ""
        for line in text.splitlines()[:200]:
            stripped = line.strip()
            if not title:
                match = re.match(r"^(?:title|book title)\s*:\s*(.+)$", stripped, flags=re.IGNORECASE)
                if match:
                    title = match.group(1).strip()
            if not author:
                match = re.match(r"^(?:author|by)\s*:\s*(.+)$", stripped, flags=re.IGNORECASE)
                if match:
                    author = match.group(1).strip()
            if title and author:
                break
        return {"title": title, "author": author, "source": "txt"}
    except Exception as exc:
        return {"error": str(exc)[:500], "source": "txt"}


def ebook_metadata(path: str) -> dict:
    suffix = Path(path).suffix.lower()
    if suffix == ".epub":
        return epub_metadata(path)
    if suffix == ".pdf":
        return pdf_metadata(path)
    if suffix in {".mobi", ".azw", ".azw3"}:
        return mobi_metadata(path)
    if suffix == ".cbz":
        return cbz_metadata(path)
    if suffix == ".rtf":
        return rtf_metadata(path)
    if suffix == ".txt":
        return txt_metadata(path)
    return {"unsupported": suffix or "unknown", "source": suffix.lstrip(".") or "unknown"}

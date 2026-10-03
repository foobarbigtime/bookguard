from __future__ import annotations

from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

from . import pdf_probe
from .archive_io import read_zip_member_bounded


ENGLISH_CODES = {"en", "eng"}
LANGUAGE_ALIASES = {
    "en-us": "en",
    "en-gb": "en",
    "en-ca": "en",
    "en-au": "en",
    "eng": "en",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "es": "es",
    "spa": "es",
    "it": "it",
    "ita": "it",
    "pt": "pt",
    "por": "pt",
    "nl": "nl",
    "nld": "nl",
    "dut": "nl",
    "sv": "sv",
    "swe": "sv",
    "no": "no",
    "nor": "no",
    "da": "da",
    "dan": "da",
    "fi": "fi",
    "fin": "fi",
    "pl": "pl",
    "pol": "pl",
    "cs": "cs",
    "ces": "cs",
    "cze": "cs",
    "ru": "ru",
    "rus": "ru",
    "uk": "uk",
    "ukr": "uk",
    "ja": "ja",
    "jpn": "ja",
    "ko": "ko",
    "kor": "ko",
    "zh": "zh",
    "zho": "zh",
    "chi": "zh",
}


def normalize_language(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if not raw or raw in {"und", "unknown", "none", "n/a"}:
        return ""
    if raw in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[raw]
    primary = raw.split("-", 1)[0]
    if primary in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[primary]
    if len(primary) in {2, 3} and primary.isalpha():
        return primary
    return ""


def epub_languages(path: str) -> dict:
    languages: set[str] = set()
    try:
        with zipfile.ZipFile(path) as zf:
            container = ET.fromstring(read_zip_member_bounded(zf, "META-INF/container.xml"))
            rootfile = None
            for elem in container.iter():
                if elem.tag.endswith("rootfile"):
                    rootfile = elem.attrib.get("full-path")
                    if rootfile:
                        break
            if not rootfile:
                return {"source": "epub_metadata", "languages": [], "evidence": []}
            package = ET.fromstring(read_zip_member_bounded(zf, rootfile))
            for elem in package.iter():
                local = elem.tag.rsplit("}", 1)[-1].lower()
                if local == "language" and elem.text:
                    language = normalize_language(elem.text)
                    if language:
                        languages.add(language)
    except Exception:
        pass
    return {
        "source": "epub_metadata",
        "languages": sorted(languages),
        "evidence": sorted(languages),
    }


def pdf_languages(path: str) -> dict:
    payload = pdf_probe.probe_pdf(path)
    language = normalize_language(payload.get("language"))
    languages = [language] if language else []
    return {
        "source": "pdf_metadata",
        "languages": languages,
        "evidence": languages,
    }


def ebook_languages(path: str) -> dict:
    suffix = Path(path).suffix.lower()
    if suffix == ".epub":
        return epub_languages(path)
    if suffix == ".pdf":
        return pdf_languages(path)
    return {"source": "unsupported_language_metadata", "languages": [], "evidence": []}


def explicit_non_english(result: dict) -> list[str]:
    languages = [normalize_language(value) for value in result.get("languages", [])]
    return sorted({language for language in languages if language and language not in ENGLISH_CODES})


LANGUAGE_NAMES = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "sv": "Swedish", "no": "Norwegian", "da": "Danish",
    "fi": "Finnish", "pl": "Polish", "cs": "Czech", "ru": "Russian", "uk": "Ukrainian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
}


def declared_language(result: dict) -> dict:
    """The language a scanned file declares about itself, for display.

    Read from what the library scan recorded (EPUB/PDF metadata or audio
    tags); nothing is opened here. ``nonEnglish`` is true only when the file
    explicitly declares a language other than English.
    """
    detection = (result.get("metadata") or {}).get("language_detection") or {}
    codes = sorted({code for code in (normalize_language(v) for v in detection.get("languages") or []) if code})
    names = [LANGUAGE_NAMES.get(code, code.upper()) for code in codes]
    return {
        "codes": codes,
        "label": " and ".join(names),
        "declared": bool(codes),
        "nonEnglish": bool(explicit_non_english({"languages": codes})),
    }

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import zipfile
import xml.etree.ElementTree as ET

from .metadata import audio_files


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


def _audio_file_languages(path: str) -> list[str]:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format_tags=language:stream=codec_type:stream_tags=language",
        "-of",
        "json",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return []

    values: list[object] = []
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    fmt_tags = fmt.get("tags") if isinstance(fmt.get("tags"), dict) else {}
    values.append(fmt_tags.get("language"))

    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        values.append(tags.get("language"))

    return sorted({language for value in values if (language := normalize_language(value))})


def audiobook_languages(path: str, sample_limit: int = 3) -> dict:
    sampled = audio_files(path, max(1, sample_limit))
    languages: set[str] = set()
    evidence: list[dict] = []
    for candidate in sampled:
        detected = _audio_file_languages(candidate)
        if detected:
            languages.update(detected)
            evidence.append({"path": candidate, "languages": detected})
    return {
        "source": "embedded_audio_metadata",
        "languages": sorted(languages),
        "evidence": evidence,
        "sampled_files": len(sampled),
    }


def epub_languages(path: str) -> dict:
    languages: set[str] = set()
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
                return {"source": "epub_metadata", "languages": [], "evidence": []}
            package = ET.fromstring(zf.read(rootfile))
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
    languages: set[str] = set()
    try:
        from pypdf import PdfReader

        reader = PdfReader(path)
        root = reader.trailer.get("/Root")
        if root:
            language = normalize_language(root.get("/Lang"))
            if language:
                languages.add(language)
    except Exception:
        pass
    return {
        "source": "pdf_metadata",
        "languages": sorted(languages),
        "evidence": sorted(languages),
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

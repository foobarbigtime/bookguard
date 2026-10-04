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
    "hun": "hu", "tur": "tr", "ara": "ar", "heb": "he", "gre": "el", "ell": "el",
    "rum": "ro", "ron": "ro", "cat": "ca", "hin": "hi", "tha": "th", "vie": "vi",
    "ind": "id", "nob": "nb", "nno": "nn", "slv": "sl", "slk": "sk", "slo": "sk",
    "hrv": "hr", "srp": "sr", "bul": "bg", "est": "et", "lav": "lv", "lit": "lt",
    "ice": "is", "isl": "is", "gle": "ga", "wel": "cy", "cym": "cy", "per": "fa",
    "fas": "fa", "afr": "af", "eus": "eu", "baq": "eu", "glg": "gl", "lat": "la",
}


# Two-letter codes must be ISO 639-1 ("un" is not). Three-letter codes are kept
# unless they are a placeholder that names no language; those count as undeclared.
NOT_A_LANGUAGE = frozenset({"und", "mul", "zxx", "mis", "xxx", "unk", "nil", "non", "nul"})
ISO_639_1 = frozenset("""
aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy
da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu
hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb
lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om
or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw
ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu
""".split())


def normalize_language(value: object) -> str:
    raw = str(value or "").strip().lower().replace("_", "-")
    if not raw:
        return ""
    if raw in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[raw]
    primary = raw.split("-", 1)[0]
    if primary in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[primary]
    if len(primary) == 2:
        return primary if primary in ISO_639_1 else ""
    if len(primary) == 3 and primary.isalpha() and primary not in NOT_A_LANGUAGE and not "qaa" <= primary <= "qtz":
        return primary  # a real ISO 639-2/3 code without a two-letter form ("ben", "urd")
    return ""


def library_languages() -> frozenset[str]:
    """Languages the user keeps (Settings → Scanning). Empty means every language."""
    from .config import settings

    return frozenset(code for code in (normalize_language(v) for v in settings.library_languages) if code)


def outside_library_languages(result: dict) -> list[str]:
    """Declared languages that are not one of the user's library languages."""
    keep = library_languages()
    if not keep:
        return []
    languages = {normalize_language(value) for value in result.get("languages", [])}
    return sorted(language for language in languages if language and language not in keep)


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
    "hu": "Hungarian", "tr": "Turkish", "ar": "Arabic", "he": "Hebrew", "el": "Greek",
    "ro": "Romanian", "ca": "Catalan", "hi": "Hindi", "th": "Thai", "vi": "Vietnamese",
    "id": "Indonesian", "nb": "Norwegian Bokmål", "nn": "Norwegian Nynorsk", "sl": "Slovenian",
    "sk": "Slovak", "hr": "Croatian", "sr": "Serbian", "bg": "Bulgarian", "et": "Estonian",
    "lv": "Latvian", "lt": "Lithuanian", "is": "Icelandic", "ga": "Irish", "cy": "Welsh",
    "fa": "Persian", "af": "Afrikaans", "eu": "Basque", "gl": "Galician", "la": "Latin",
    "bn": "Bengali", "ur": "Urdu", "ta": "Tamil", "ms": "Malay", "tl": "Tagalog", "sw": "Swahili",
}


def parse_language_list(raw: object) -> tuple[list[str], list[str]]:
    """Codes from "English, nl" or a list, plus every entry that names no language."""
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    if not isinstance(raw, list):
        return [], []
    by_name = {name.casefold(): code for code, name in LANGUAGE_NAMES.items()}
    codes: list[str] = []
    unknown: list[str] = []
    for part in raw:
        text = str(part).strip()
        if not text:
            continue
        code = by_name.get(text.casefold()) or normalize_language(text)
        if not code:
            unknown.append(text)
        elif code not in codes:
            codes.append(code)
    return codes, unknown


def declared_language(result: dict) -> dict:
    """The language a scanned file declares about itself, for display.

    Read from what the library scan recorded (EPUB/PDF metadata or audio
    tags); nothing is opened here. ``otherLanguage`` is true only when the file
    explicitly declares a language that is not one of the library languages.
    """
    detection = (result.get("metadata") or {}).get("language_detection") or {}
    codes = sorted({code for code in (normalize_language(v) for v in detection.get("languages") or []) if code})
    names = [LANGUAGE_NAMES.get(code, code.upper()) for code in codes]
    return {
        "codes": codes,
        "label": " and ".join(names),
        "declared": bool(codes),
        "otherLanguage": bool(outside_library_languages({"languages": codes})),
    }

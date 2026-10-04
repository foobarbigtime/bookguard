import zipfile

import app.archive_io as archive_io
from app.language_detection import epub_languages, explicit_non_english, normalize_language, pdf_languages
from pypdf import PdfWriter
from pypdf.generic import NameObject, TextStringObject
from app.scanner import _language_override


def test_normalize_language_aliases():
    assert normalize_language("eng") == "en"
    assert normalize_language("en-US") == "en"
    assert normalize_language("deu") == "de"
    assert normalize_language("ger") == "de"
    assert normalize_language("fra") == "fr"


def test_explicit_non_english_ignores_english():
    assert explicit_non_english({"languages": ["en", "eng", "en-CA"]}) == []


def test_explicit_non_english_returns_other_languages():
    assert explicit_non_english({"languages": ["eng", "deu", "fr"]}) == ["de", "fr"]


def test_language_override_escalates_pass_to_review():
    base = {
        "classification": "PASS",
        "risk_score": 0,
        "reason_code": "MATCH",
        "reasons": ["Expected title and author matched."],
        "metadata": {},
    }

    result = _language_override(base, {"languages": ["deu"]})

    assert result["classification"] == "REVIEW"
    assert result["risk_score"] == 90
    assert result["reason_code"] == "NON_ENGLISH_LANGUAGE"
    assert result["metadata"]["policy_flags"] == ["NON_ENGLISH_LANGUAGE"]
    assert result["reasons"][0] == "Expected title and author matched."
    assert "declares German, which is not one of your library languages" in result["reasons"][-1]


def test_language_override_never_downgrades_reject():
    base = {
        "classification": "REJECT",
        "risk_score": 100,
        "reason_code": "STRONG_MISMATCH",
        "reasons": ["Strong mismatch evidence."],
        "metadata": {},
    }

    result = _language_override(base, {"languages": ["fra"]})

    assert result["classification"] == "REJECT"
    assert result["risk_score"] == 100
    assert result["reason_code"] == "STRONG_MISMATCH"
    assert result["metadata"]["policy_flags"] == ["NON_ENGLISH_LANGUAGE"]
    assert result["reasons"][0] == "Strong mismatch evidence."
    assert "declares French, which is not one of your library languages" in result["reasons"][-1]


def test_epub_language_is_read_from_package_metadata(tmp_path):
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            """<?xml version='1.0'?>
            <container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>
              <rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles>
            </container>""",
        )
        zf.writestr(
            "OEBPS/content.opf",
            """<?xml version='1.0'?>
            <package xmlns='http://www.idpf.org/2007/opf'
                     xmlns:dc='http://purl.org/dc/elements/1.1/'>
              <metadata><dc:title>Test</dc:title><dc:language>de-DE</dc:language></metadata>
            </package>""",
        )

    result = epub_languages(str(path))
    assert result["languages"] == ["de"]
    assert explicit_non_english(result) == ["de"]


def test_epub_language_read_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_io, "DEFAULT_XML_MEMBER_LIMIT", 512)
    path = tmp_path / "oversized-language.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            """<container>
              <rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles>
            </container>""",
        )
        zf.writestr(
            "OEBPS/content.opf",
            "<package><metadata><language>de</language>"
            + ("x" * 2048)
            + "</metadata></package>",
        )

    result = epub_languages(str(path))

    assert result["languages"] == []


def test_pdf_language_is_read_in_isolated_probe(tmp_path):
    path = tmp_path / "book.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer._root_object.update({NameObject("/Lang"): TextStringObject("de-DE")})
    with path.open("wb") as handle:
        writer.write(handle)

    result = pdf_languages(str(path))

    assert result["languages"] == ["de"]
    assert result["evidence"] == ["de"]


def test_junk_language_codes_count_as_undeclared():
    assert [normalize_language(code) for code in ["xxx", "un", "zxx", "mul", "und", "swe", "en-US"]] == [
        "", "", "", "", "", "sv", "en",
    ]


def test_library_languages_decide_what_is_flagged(monkeypatch):
    from app.config import settings
    from app.language_detection import outside_library_languages

    result = {"languages": ["eng", "nl", "de"]}
    monkeypatch.setattr(settings, "library_languages", ["en"])
    assert outside_library_languages(result) == ["de", "nl"]
    monkeypatch.setattr(settings, "library_languages", ["en", "nl"])
    assert outside_library_languages(result) == ["de"]
    monkeypatch.setattr(settings, "library_languages", [])
    assert outside_library_languages(result) == []  # keep every language


def test_library_languages_setting_accepts_names_and_codes():
    from app.config import Settings

    configured = Settings()
    configured.apply({"library_languages": "English, nl, xxx, Dutch"})
    assert configured.library_languages == ["en", "nl"]
    configured.apply({"library_languages": ""})
    assert configured.library_languages == []

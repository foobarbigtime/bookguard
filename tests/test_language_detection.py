import zipfile

from app.language_detection import epub_languages, explicit_non_english, normalize_language


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

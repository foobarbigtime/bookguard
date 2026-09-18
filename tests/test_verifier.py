import zipfile

from app.verification_engine import classify_identity
from app.verifier import verified_repair_preview


def sample_result():
    return {
        "id": 1,
        "scan_id": "scan-1",
        "file_id": 10,
        "book_id": 20,
        "classification": "REVIEW",
        "reason_code": "MISMATCH",
        "format": "ebook",
        "author": "Aaron Blabey",
        "title": "Cat of Death!",
        "stored_path": "/data/media/books/Aaron Blabey/Cat of Death.epub",
        "local_path": "/books/Aaron Blabey/Cat of Death.epub",
        "reasons": ["Embedded metadata does not match"],
        "metadata": {"title": "Death of a Texan", "author": "Cat Hickey"},
    }


def classify(metadata, text):
    verdict, confidence, evidence = classify_identity(
        sample_result(), metadata, text, [], []
    )
    return verdict, confidence, evidence


def test_wrong_content_requires_content_support_for_conflicting_identity():
    verdict, confidence, evidence = classify(
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        "Death of a Texan. A novel by Cat Hickey. Copyright Cat Hickey.",
    )
    assert verdict == "WRONG_CONTENT"
    assert confidence == 99
    assert evidence["content"]["embedded_title_found"] is True
    assert evidence["content"]["embedded_author_found"] is True
    assert evidence["content"]["expected_title_found"] is False
    assert evidence["content"]["expected_author_found"] is False


def test_metadata_error_requires_expected_title_and_author_in_content():
    verdict, confidence, evidence = classify(
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        "Cat of Death! by Aaron Blabey. Copyright Aaron Blabey.",
    )
    assert verdict == "METADATA_ERROR"
    assert confidence >= 90
    assert evidence["content"]["expected_title_found"] is True
    assert evidence["content"]["expected_author_found"] is True
    assert evidence["content"]["embedded_title_found"] is False
    assert evidence["content"]["embedded_author_found"] is False


def test_verified_correct_needs_matching_metadata_and_content():
    verdict, confidence, evidence = classify(
        {"title": "Cat of Death!", "author": "Aaron Blabey"},
        "Cat of Death! by Aaron Blabey. Written and illustrated by Aaron Blabey.",
    )
    assert verdict == "VERIFIED_CORRECT"
    assert confidence == 99
    assert evidence["metadata_matches_expected"] is True


def test_mixed_identity_stays_ambiguous():
    verdict, confidence, evidence = classify(
        {"title": "Death of a Texan", "author": "Cat Hickey"},
        "Cat of Death! by Aaron Blabey. Also includes Death of a Texan by Cat Hickey.",
    )
    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert confidence < 90
    assert evidence["content"]["expected_title_found"] is True
    assert evidence["content"]["embedded_title_found"] is True


def _make_missing_metadata_epub(path):
    container = """<?xml version='1.0'?>
<container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>
  <rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles>
</container>"""
    package = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns='http://www.idpf.org/2007/opf'
         xmlns:dc='http://purl.org/dc/elements/1.1/' version='3.0'>
  <metadata/>
  <manifest/>
  <spine/>
</package>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)


def _missing_metadata_verification(confidence=97, strong=True):
    return {
        "id": 55,
        "verdict": "METADATA_ERROR",
        "confidence": confidence,
        "evidence": {
            "embedded": {"title": "", "author": ""},
            "content": {
                "expected_signal": {
                    "strong_identity": strong,
                    "front_proximity": strong,
                    "front_proximity_chars": 20 if strong else None,
                    "title_first_position": 1 if strong else None,
                }
            },
            "metadata_matches_expected": False,
        },
    }


def test_verified_repair_preview_allows_independently_verified_missing_metadata(tmp_path):
    path = tmp_path / "bel-canto.epub"
    _make_missing_metadata_epub(path)
    result = sample_result()
    result.update(
        {
            "reason_code": "NO_METADATA",
            "title": "Bel Canto",
            "author": "Ann Patchett",
            "local_path": str(path),
            "metadata": {"title": "", "author": "", "source": "epub"},
        }
    )

    preview = verified_repair_preview(
        result,
        _missing_metadata_verification(),
    )

    assert preview["eligible"] is True
    assert preview["safe"] is True
    assert preview["repair_reason_code"] == "MISSING_METADATA"
    assert preview["missing_metadata"] is True
    assert preview["before"]["title"] == ""
    assert preview["before"]["author"] == ""
    assert preview["after"]["title"] == "Bel Canto"
    assert preview["after"]["author"] == "Ann Patchett"


def test_verified_missing_metadata_requires_strong_97_percent_identity(tmp_path):
    path = tmp_path / "bel-canto.epub"
    _make_missing_metadata_epub(path)
    result = sample_result()
    result.update(
        {
            "reason_code": "NO_METADATA",
            "title": "Bel Canto",
            "author": "Ann Patchett",
            "local_path": str(path),
            "metadata": {"title": "", "author": "", "source": "epub"},
        }
    )

    preview = verified_repair_preview(
        result,
        _missing_metadata_verification(confidence=96),
    )

    assert preview["eligible"] is False
    assert preview["safe"] is False
    assert "97% confidence" in preview["reason"]

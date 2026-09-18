import struct
import zipfile
from pathlib import Path

import app.archive_io as archive_io
import app.metadata as metadata
import app.pdf_probe as pdf_probe
from app.metadata import cbz_metadata, epub_metadata, mobi_metadata, pdf_metadata, rtf_metadata, txt_metadata
from pypdf import PdfWriter


def _u32(value: int) -> bytes:
    return struct.pack(">I", value)


def _exth_record(record_type: int, value: str) -> bytes:
    payload = value.encode("utf-8")
    return _u32(record_type) + _u32(len(payload) + 8) + payload


def build_test_mobi(path: Path) -> None:
    record = bytearray(420)
    mobi = 16
    header_length = 0xE8
    record[mobi:mobi + 4] = b"MOBI"
    record[mobi + 4:mobi + 8] = _u32(header_length)
    record[mobi + 0x0C:mobi + 0x10] = _u32(65001)
    record[mobi + 0x70:mobi + 0x74] = _u32(0x40)

    fallback_title = b"Fallback Title"
    full_name_offset = 380
    record[mobi + 0x44:mobi + 0x48] = _u32(full_name_offset)
    record[mobi + 0x48:mobi + 0x4C] = _u32(len(fallback_title))
    record[full_name_offset:full_name_offset + len(fallback_title)] = fallback_title

    records = [
        _exth_record(100, "Daniel Silva"),
        _exth_record(503, "The English Girl"),
    ]
    exth_body = b"".join(records)
    exth = b"EXTH" + _u32(12 + len(exth_body)) + _u32(len(records)) + exth_body
    exth_start = mobi + header_length
    record[exth_start:exth_start + len(exth)] = exth

    pdb = bytearray(86)
    pdb[76:78] = struct.pack(">H", 1)
    pdb[78:82] = _u32(86)
    path.write_bytes(bytes(pdb) + bytes(record))


def test_mobi_metadata_reads_exth(tmp_path):
    path = tmp_path / "book.azw3"
    build_test_mobi(path)
    md = mobi_metadata(str(path))
    assert md["title"] == "The English Girl"
    assert md["author"] == "Daniel Silva"


def test_rtf_metadata(tmp_path):
    path = tmp_path / "book.rtf"
    path.write_text(r"{\rtf1{\info{\title Test Book}{\author Test Author}} body}", encoding="latin-1")
    md = rtf_metadata(str(path))
    assert md["title"] == "Test Book"
    assert md["author"] == "Test Author"


def test_txt_metadata_requires_explicit_headers(tmp_path):
    path = tmp_path / "wrong-name.txt"
    path.write_text("Title: Real Book\nAuthor: Real Author\n\nChapter One", encoding="utf-8")
    md = txt_metadata(str(path))
    assert md["title"] == "Real Book"
    assert md["author"] == "Real Author"


def test_cbz_comicinfo(tmp_path):
    path = tmp_path / "comic.cbz"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ComicInfo.xml", "<ComicInfo><Title>Drama</Title><Writer>Raina Telgemeier</Writer></ComicInfo>")
    md = cbz_metadata(str(path))
    assert md["title"] == "Drama"
    assert md["author"] == "Raina Telgemeier"


def test_epub_metadata_reads_title_and_author(tmp_path):
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
              <metadata>
                <dc:title>The English Girl</dc:title>
                <dc:creator>Daniel Silva</dc:creator>
              </metadata>
            </package>""",
        )

    md = epub_metadata(str(path))

    assert md["title"] == "The English Girl"
    assert md["author"] == "Daniel Silva"


def test_epub_metadata_rejects_oversized_package_xml(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_io, "DEFAULT_XML_MEMBER_LIMIT", 512)
    path = tmp_path / "oversized.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            """<container>
              <rootfiles><rootfile full-path='OEBPS/content.opf'/></rootfiles>
            </container>""",
        )
        zf.writestr(
            "OEBPS/content.opf",
            "<package><metadata>" + ("x" * 2048) + "</metadata></package>",
        )

    md = epub_metadata(str(path))

    assert md["source"] == "epub"
    assert "error" in md
    assert "exceeds the 512-byte metadata limit" in md["error"]


def test_pdf_metadata_reads_in_isolated_probe(tmp_path):
    path = tmp_path / "book.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": "The English Girl", "/Author": "Daniel Silva"})
    with path.open("wb") as handle:
        writer.write(handle)

    md = pdf_metadata(str(path))

    assert md["title"] == "The English Girl"
    assert md["author"] == "Daniel Silva"
    assert md["source"] == "pdf"


def test_pdf_metadata_refuses_oversized_routine_parse(tmp_path, monkeypatch):
    path = tmp_path / "oversized.pdf"
    path.write_bytes(b"%PDF-1.7\n" + (b"x" * 128) + b"\n%%EOF\n")
    monkeypatch.setattr(pdf_probe, "MAX_ROUTINE_PDF_BYTES", 64)

    md = pdf_metadata(str(path))

    assert md["source"] == "pdf"
    assert "error" in md
    assert "routine metadata limit" in md["error"]


def test_mobi_metadata_refuses_oversized_record_zero(tmp_path, monkeypatch):
    path = tmp_path / "oversized.azw3"
    build_test_mobi(path)
    monkeypatch.setattr(metadata, "MAX_MOBI_RECORD0_BYTES", 128)

    md = mobi_metadata(str(path))

    assert md["source"] == "mobi"
    assert "error" in md
    assert "routine metadata limit" in md["error"]



def test_txt_metadata_reads_only_bounded_prefix(tmp_path, monkeypatch):
    path = tmp_path / "book.txt"
    path.write_bytes(
        b"Title: Real Book\nAuthor: Real Author\n"
        + (b"x" * (1024 * 1024))
    )

    observed = {}
    real_reader = metadata.read_file_prefix

    def tracked_reader(target, *, max_bytes):
        observed["max_bytes"] = max_bytes
        return real_reader(target, max_bytes=max_bytes)

    monkeypatch.setattr(metadata, "read_file_prefix", tracked_reader)

    md = txt_metadata(str(path))

    assert observed["max_bytes"] == 131072
    assert md["title"] == "Real Book"
    assert md["author"] == "Real Author"


def test_rtf_metadata_reads_only_bounded_prefix(tmp_path, monkeypatch):
    path = tmp_path / "book.rtf"
    path.write_bytes(
        rb"{\rtf1{\info{\title Test Book}{\author Test Author}} "
        + (b"x" * (1024 * 1024))
        + b"}"
    )

    observed = {}
    real_reader = metadata.read_file_prefix

    def tracked_reader(target, *, max_bytes):
        observed["max_bytes"] = max_bytes
        return real_reader(target, max_bytes=max_bytes)

    monkeypatch.setattr(metadata, "read_file_prefix", tracked_reader)

    md = rtf_metadata(str(path))

    assert observed["max_bytes"] == 262144
    assert md["title"] == "Test Book"
    assert md["author"] == "Test Author"

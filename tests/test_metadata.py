import struct
import zipfile
from pathlib import Path

from app.metadata import cbz_metadata, mobi_metadata, rtf_metadata, txt_metadata


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

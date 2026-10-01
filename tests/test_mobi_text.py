"""Kindle (MOBI/AZW/AZW3) text reading.

The tests build real PalmDB/MOBI files in all three compression schemes, so the
reader is exercised on genuine binary structure rather than stand-ins.
"""

from __future__ import annotations

import struct

import pytest

from app import mobi_text
from app.ebook_extraction import extract_ebook_identity
from app.mobi_text import MobiError, palmdoc_decompress, read_mobi_text
from app.verification_engine import classify_identity

TITLE_PAGE = (
    "<html><body><h1>The Feros</h1><p>by Wesley King</p>"
    "<p>G. P. Putnam&#8217;s Sons &amp; Penguin</p>"
    + "<p>A fictional chapter of story text.</p>" * 60
    + "</body></html>"
).encode("utf-8")


def palmdoc_literal(data: bytes) -> bytes:
    """Valid PalmDOC using only literal runs (the decoder's generic path)."""
    out = bytearray()
    for i in range(0, len(data), 8):
        chunk = data[i:i + 8]
        out.append(len(chunk))
        out += chunk
    return bytes(out)


def huff_tables(phrases: list[tuple[bytes, bool]]) -> tuple[bytes, bytes, dict[int, int]]:
    """A HUFF/CDIC pair with fixed 8-bit codes: byte b selects phrase 255 - b."""
    table1 = b"".join(struct.pack(">L", (255 << 8) | 0x80 | 8) for _ in range(256))
    table2 = b"\x00" * (64 * 4)
    huff = (
        b"HUFF\x00\x00\x00\x18" + struct.pack(">LL", 24, 24 + len(table1)) + b"\x00" * 8
        + table1 + table2
    )
    entries = b""
    offsets = []
    base = len(phrases) * 2
    for phrase, literal in phrases:
        offsets.append(base + len(entries))
        entries += struct.pack(">H", len(phrase) | (0x8000 if literal else 0)) + phrase
    cdic = (
        b"CDIC\x00\x00\x00\x10"
        + struct.pack(">LL", len(phrases), 8)
        + b"".join(struct.pack(">H", o) for o in offsets)
        + entries
    )
    return huff, cdic, {index: 255 - index for index in range(len(phrases))}


def build_mobi(
    text_records: list[bytes],
    *,
    compression: int = 1,
    encryption: int = 0,
    title: str = "The Feros",
    author: str = "Wesley King",
    isbn: str = "",
    extra_flags: int = 0,
    extra_records: list[bytes] = (),
    huff_first: int = 0,
    huff_count: int = 0,
    header: bytes = b"BOOKMOBI",
) -> bytes:
    exth_items = [(100, author.encode()), (503, title.encode())]
    if isbn:
        exth_items.append((104, isbn.encode()))
    exth_body = b"".join(struct.pack(">LL", kind, 8 + len(v)) + v for kind, v in exth_items)
    exth = b"EXTH" + struct.pack(">LL", 12 + len(exth_body), len(exth_items)) + exth_body
    exth += b"\x00" * (-len(exth) % 4)
    mobi_length = 0xE8
    full_name = title.encode()
    full_name_offset = 16 + mobi_length + len(exth)
    mobi = bytearray(mobi_length)
    mobi[0:4] = b"MOBI"
    struct.pack_into(">L", mobi, 4, mobi_length)
    struct.pack_into(">L", mobi, 8, 2)
    struct.pack_into(">L", mobi, 12, 65001)
    struct.pack_into(">L", mobi, 20, 6)
    struct.pack_into(">LL", mobi, 0x54 - 16, full_name_offset, len(full_name))
    struct.pack_into(">LL", mobi, 0x70 - 16, huff_first, huff_count)
    struct.pack_into(">L", mobi, 0x80 - 16, 0x40)
    struct.pack_into(">H", mobi, 0xF2 - 16, extra_flags)
    palmdoc = struct.pack(">HHLHHHH", compression, 0, 0, len(text_records), 4096, encryption, 0)
    record0 = palmdoc + bytes(mobi) + exth + full_name + b"\x00\x00"
    records = [record0, *text_records, *extra_records]
    table_start = 78
    data_start = table_start + len(records) * 8 + 2
    offsets, pos = [], data_start
    for rec in records:
        offsets.append(pos)
        pos += len(rec)
    pdb = bytearray(78)
    pdb[0:len(title[:31])] = title[:31].encode()
    pdb[60:68] = header
    struct.pack_into(">H", pdb, 76, len(records))
    table = b"".join(struct.pack(">LL", o, i) for i, o in enumerate(offsets))
    return bytes(pdb) + table + b"\x00\x00" + b"".join(records)


def write(tmp_path, name: str, data: bytes):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- decompression ---------------------------------------------------------


def test_palmdoc_decodes_literals_back_references_and_space_pairs():
    encoded = b"abc" + bytes([0x80, 0x18]) + bytes([0xE1]) + b"\x02xy"
    assert palmdoc_decompress(encoded) == b"abcabc axy"


def test_palmdoc_rejects_a_reference_before_the_start():
    with pytest.raises(MobiError, match="before the start"):
        palmdoc_decompress(b"a" + bytes([0x80, 0x18]))


def test_palmdoc_stops_at_the_output_limit():
    # One literal, then repeated maximal back-references: a decompression bomb.
    bomb = b"a" + bytes([0x80, 0x0F]) * 10_000
    with pytest.raises(MobiError, match="safety limit"):
        palmdoc_decompress(bomb, limit=1000)


# --- whole files -----------------------------------------------------------


@pytest.mark.parametrize("suffix", [".mobi", ".azw3", ".azw"])
def test_uncompressed_book_text_is_read(tmp_path, suffix):
    path = write(tmp_path, "book" + suffix, build_mobi([TITLE_PAGE[:4000], TITLE_PAGE[4000:]]))
    result = read_mobi_text(path, max_chars=100_000)
    assert result.compression == "none"
    assert result.text.startswith("The Feros\nby Wesley King")
    assert "Putnam\u2019s Sons & Penguin" in result.text


def test_palmdoc_book_text_is_read(tmp_path):
    records = [palmdoc_literal(TITLE_PAGE[i:i + 4096]) for i in range(0, len(TITLE_PAGE), 4096)]
    path = write(tmp_path, "book.mobi", build_mobi(records, compression=2))
    result = read_mobi_text(path, max_chars=100_000)
    assert result.compression == "palmdoc"
    assert result.text.startswith("The Feros\nby Wesley King")


def test_huff_cdic_book_text_is_read_including_nested_phrases(tmp_path):
    words = [b"<h1>The Feros</h1>", b"<p>by Wesley King</p>", b"<p>Chapter text.</p>"]
    literal_huff, _, code = huff_tables([(w, True) for w in words])
    # Phrase 3 is itself compressed: it expands to phrases 2, 2, 2.
    nested = bytes([code[2]] * 3)
    huff, cdic, code = huff_tables([(w, True) for w in words] + [(nested, False)])
    stream = bytes([code[0], code[1], code[3], code[3]])
    path = write(tmp_path, "book.azw3", build_mobi(
        [stream], compression=17480, extra_records=[huff, cdic], huff_first=2, huff_count=2,
    ))
    result = read_mobi_text(path, max_chars=100_000)
    assert result.compression == "huff/cdic"
    assert result.text.startswith("The Feros\nby Wesley King\nChapter text.")
    assert result.text.count("Chapter text.") == 6


def test_a_self_referencing_huff_phrase_is_refused(tmp_path):
    huff, cdic, code = huff_tables([(bytes([255]), False)])
    path = write(tmp_path, "book.azw3", build_mobi(
        [bytes([code[0]])], compression=17480, extra_records=[huff, cdic], huff_first=2, huff_count=2,
    ))
    with pytest.raises(MobiError, match="refers to itself"):
        read_mobi_text(path, max_chars=100_000)


def test_trailing_record_data_is_removed_before_decoding(tmp_path):
    # Multibyte marker (bit 0) then a 5-byte trailing entry (bit 1).
    record = TITLE_PAGE[:500] + b"\xaa\x01" + b"\x00\x00\x00\x00\x85"
    path = write(tmp_path, "book.mobi", build_mobi([record], extra_flags=0b11))
    result = read_mobi_text(path, max_chars=100_000)
    assert "\xaa" not in result.text and not result.text.endswith("\x01")
    assert result.text.startswith("The Feros")


def test_drm_protected_book_reports_encryption_and_yields_no_text(tmp_path):
    path = write(tmp_path, "book.azw", build_mobi([b"\x93\x17 scrambled bytes"], encryption=2))
    result = read_mobi_text(path, max_chars=100_000)
    assert result.encrypted is True
    assert result.text == ""
    assert "DRM-protected" in result.notes[0]


def test_isbn_and_text_limit_are_honoured(tmp_path):
    path = write(tmp_path, "book.mobi", build_mobi([TITLE_PAGE], isbn="9780399256981"))
    result = read_mobi_text(path, max_chars=40)
    assert result.identifiers == ["isbn:9780399256981"]
    assert len(result.text) <= 40


def test_malformed_record_table_is_refused(tmp_path):
    data = bytearray(build_mobi([TITLE_PAGE[:1000]]))
    struct.pack_into(">L", data, 78 + 8, 10)  # record 1 now starts inside the header
    path = write(tmp_path, "book.mobi", bytes(data))
    with pytest.raises(MobiError):
        read_mobi_text(path, max_chars=100_000)


def test_non_kindle_file_is_refused(tmp_path):
    path = write(tmp_path, "book.mobi", build_mobi([TITLE_PAGE[:1000]], header=b"NOTAMOBI"))
    with pytest.raises(MobiError, match="not a PalmDB Kindle book"):
        read_mobi_text(path, max_chars=100_000)


# --- verification pipeline --------------------------------------------------


def test_header_metadata_is_read_from_a_real_multi_record_file(tmp_path):
    # Regression: record 0 was read one byte long, so every real Kindle file
    # (always more than one record) was reported as truncated with no metadata.
    from app.metadata import mobi_metadata

    path = write(tmp_path, "book.azw3", build_mobi([TITLE_PAGE[:1000], TITLE_PAGE[1000:2000]]))
    assert mobi_metadata(str(path)) == {
        "title": "The Feros", "author": "Wesley King", "source": "mobi",
    }


def test_kindle_book_now_verifies_from_its_own_text(tmp_path):
    records = [palmdoc_literal(TITLE_PAGE[i:i + 4096]) for i in range(0, len(TITLE_PAGE), 4096)]
    path = write(tmp_path, "The Feros - Wesley King.mobi", build_mobi(records, compression=2))

    extracted = extract_ebook_identity(str(path))
    verdict, confidence, _evidence = classify_identity(
        {"title": "The Feros", "author": "Wesley King"},
        extracted.metadata, extracted.text, extracted.identifiers, extracted.notes,
        extracted.front_text,
    )

    assert extracted.source == "native-mobi"
    assert extracted.metadata["title"] == "The Feros"
    assert (verdict, confidence) == ("VERIFIED_CORRECT", 99)


def test_drm_protected_kindle_book_stays_undecided_with_an_explanation(tmp_path):
    path = write(tmp_path, "The Feros - Wesley King.azw", build_mobi([b"\x93 locked"], encryption=2))

    extracted = extract_ebook_identity(str(path))
    verdict, _confidence, _evidence = classify_identity(
        {"title": "The Feros", "author": "Wesley King"},
        extracted.metadata, extracted.text, extracted.identifiers, extracted.notes,
        extracted.front_text,
    )

    assert verdict == "INSUFFICIENT_EVIDENCE"
    assert any("DRM-protected" in note for note in extracted.notes)


def test_a_broken_kindle_file_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(mobi_text, "MAX_RECORD_BYTES", 10)
    path = write(tmp_path, "book.mobi", build_mobi([TITLE_PAGE[:1000]]))

    extracted = extract_ebook_identity(str(path))

    assert extracted.text == ""
    assert any("Native extraction error" in note for note in extracted.notes)

"""Read the text of MOBI, AZW and AZW3 (Kindle) ebooks for identity evidence.

Kindle files are PalmDB databases. Record 0 carries the headers; the book text
follows in records compressed with PalmDOC (LZ77) or HUFF/CDIC (Huffman with a
phrase dictionary). This reader only ever produces text for identity checks: it
never writes anything and never trusts an offset, length, or count from the file
without checking it against the file and a fixed limit first.

A DRM-protected book reports ``encrypted`` and yields no text; its header
metadata (read elsewhere) remains usable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import html
from pathlib import Path
import re
import struct

MAX_RECORDS = 65_535
MAX_RECORD0_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_RECORD_OUTPUT = 256 * 1024
MAX_HUFF_DEPTH = 32
MAX_CDIC_PHRASES = 1 << 20

COMPRESSION_NONE = 1
COMPRESSION_PALMDOC = 2
COMPRESSION_HUFFCDIC = 17480


class MobiError(ValueError):
    """The file is not a readable Kindle book or breaks a safety limit."""


@dataclass
class MobiText:
    text: str = ""
    encrypted: bool = False
    compression: str = ""
    identifiers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise MobiError("A Kindle header field points outside its record.")
    return struct.unpack_from(">H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise MobiError("A Kindle header field points outside its record.")
    return struct.unpack_from(">I", data, offset)[0]


def palmdoc_decompress(data: bytes, limit: int = MAX_RECORD_OUTPUT) -> bytes:
    """PalmDOC LZ77, with a hard output limit and validated back-references."""
    out = bytearray()
    i = 0
    size = len(data)
    while i < size:
        c = data[i]
        i += 1
        if 1 <= c <= 8:
            out += data[i:i + c]
            i += c
        elif c < 0x80:
            out.append(c)
        elif c >= 0xC0:
            out.append(0x20)
            out.append(c ^ 0x80)
        else:
            if i >= size:
                raise MobiError("A PalmDOC back-reference is truncated.")
            pair = (c << 8) | data[i]
            i += 1
            distance = (pair >> 3) & 0x07FF
            length = (pair & 0x07) + 3
            if distance < 1 or distance > len(out):
                raise MobiError("A PalmDOC back-reference points before the start of the text.")
            for _ in range(length):
                out.append(out[-distance])
        if len(out) > limit:
            raise MobiError("A Kindle text record expands beyond the safety limit.")
    return bytes(out)


class HuffCdicReader:
    """HUFF/CDIC decoder with bounded dictionary size, recursion, and output."""

    def __init__(self, huff: bytes, cdics: list[bytes]) -> None:
        if huff[:8] != b"HUFF\x00\x00\x00\x18" or len(huff) < 24:
            raise MobiError("The HUFF record header is invalid.")
        table1, table2 = struct.unpack_from(">LL", huff, 8)
        if table1 + 256 * 4 > len(huff) or table2 + 64 * 4 > len(huff):
            raise MobiError("The HUFF tables point outside the record.")
        self.dict1 = []
        for value in struct.unpack_from(">256L", huff, table1):
            codelen, term, maxcode = value & 0x1F, value & 0x80, value >> 8
            if codelen == 0 or (codelen <= 8 and not term):
                raise MobiError("The HUFF code table is invalid.")
            self.dict1.append((codelen, term, ((maxcode + 1) << (32 - codelen)) - 1))
        pairs = struct.unpack_from(">64L", huff, table2)
        self.mincode = [0] + [low << (32 - n) for n, low in enumerate(pairs[0::2], start=1)]
        self.maxcode = [0] + [((high + 1) << (32 - n)) - 1 for n, high in enumerate(pairs[1::2], start=1)]
        self.phrases: list[list] = []
        for cdic in cdics:
            self._load_cdic(cdic)

    def _load_cdic(self, cdic: bytes) -> None:
        if cdic[:8] != b"CDIC\x00\x00\x00\x10" or len(cdic) < 16:
            raise MobiError("A CDIC record header is invalid.")
        total, bits = struct.unpack_from(">LL", cdic, 8)
        if total > MAX_CDIC_PHRASES or bits > 20:
            raise MobiError("The CDIC dictionary exceeds the safety limit.")
        count = min(1 << bits, total - len(self.phrases))
        if count < 0 or 16 + count * 2 > len(cdic):
            raise MobiError("The CDIC offset table is truncated.")
        for offset in struct.unpack_from(f">{count}H", cdic, 16):
            start = 16 + offset
            if start + 2 > len(cdic):
                raise MobiError("A CDIC phrase points outside the record.")
            header = struct.unpack_from(">H", cdic, start)[0]
            phrase = cdic[start + 2:start + 2 + (header & 0x7FFF)]
            if len(phrase) != header & 0x7FFF:
                raise MobiError("A CDIC phrase is truncated.")
            # [bytes, literal?, being-expanded?]
            self.phrases.append([phrase, bool(header & 0x8000), False])

    def unpack(self, data: bytes, depth: int = 0, limit: int = MAX_RECORD_OUTPUT) -> bytes:
        if depth > MAX_HUFF_DEPTH:
            raise MobiError("HUFF/CDIC phrases nest beyond the safety limit.")
        bits_left = len(data) * 8
        padded = data + b"\x00" * 8
        pos = 0
        x = struct.unpack_from(">Q", padded, pos)[0]
        n = 32
        out = bytearray()
        while True:
            if n <= 0:
                pos += 4
                x = struct.unpack_from(">Q", padded, pos)[0]
                n += 32
            code = (x >> n) & 0xFFFFFFFF
            codelen, term, maxcode = self.dict1[code >> 24]
            if not term:
                while codelen < 32 and code < self.mincode[codelen]:
                    codelen += 1
                if codelen >= len(self.maxcode):
                    raise MobiError("A HUFF code is invalid.")
                maxcode = self.maxcode[codelen]
            n -= codelen
            bits_left -= codelen
            if bits_left < 0:
                break
            index = (maxcode - code) >> (32 - codelen)
            if not 0 <= index < len(self.phrases):
                raise MobiError("A HUFF code refers to a missing phrase.")
            entry = self.phrases[index]
            if not entry[1]:
                if entry[2]:
                    raise MobiError("A CDIC phrase refers to itself.")
                entry[2] = True
                entry[0] = self.unpack(entry[0], depth + 1, limit)
                entry[1], entry[2] = True, False
            out += entry[0]
            if len(out) > limit:
                raise MobiError("A Kindle text record expands beyond the safety limit.")
        return bytes(out)


def _trailing_size(record: bytes, flags: int) -> int:
    """Bytes of per-record trailing data to drop before decompression."""
    size = len(record)
    total = 0
    remaining = flags >> 1
    while remaining:
        if remaining & 1:
            value = 0
            end = size - total
            for byte in record[max(0, end - 4):end]:
                if byte & 0x80:
                    value = 0
                value = (value << 7) | (byte & 0x7F)
            total += value
        remaining >>= 1
    if flags & 1 and size - total - 1 >= 0:
        total += (record[size - total - 1] & 0x3) + 1
    if total > size:
        raise MobiError("A Kindle text record's trailing data is larger than the record.")
    return total


_BLOCK_TAGS = re.compile(r"<\s*(?:br|/p|/div|/h[1-6]|/li|/tr|/title)\b[^>]*>", re.I)


def _html_to_text(markup: str) -> str:
    markup = re.sub(r"<(script|style)\b.*?</\1\s*>", " ", markup, flags=re.I | re.S)
    markup = _BLOCK_TAGS.sub("\n", markup)
    markup = re.sub(r"<[^>]+>", " ", markup)
    text = html.unescape(markup)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def read_mobi_text(path: str | Path, max_chars: int) -> MobiText:
    """Return the leading text of a Kindle book, up to ``max_chars`` characters."""
    target = Path(path)
    file_size = target.stat().st_size
    with target.open("rb") as handle:
        header = handle.read(78)
        if len(header) < 78 or header[60:68] not in {b"BOOKMOBI", b"TEXtREAd"}:
            raise MobiError("The file is not a PalmDB Kindle book.")
        count = struct.unpack_from(">H", header, 76)[0]
        if count < 2 or count > MAX_RECORDS:
            raise MobiError("The Kindle record count is outside the safety limit.")
        table = handle.read(count * 8)
        if len(table) != count * 8:
            raise MobiError("The Kindle record table is truncated.")
        offsets = [struct.unpack_from(">I", table, i * 8)[0] for i in range(count)] + [file_size]
        if any(b < a for a, b in zip(offsets, offsets[1:])) or offsets[0] < 78 + count * 8:
            raise MobiError("The Kindle record offsets are out of order or overlap the header.")

        def record(index: int, cap: int = MAX_RECORD_BYTES) -> bytes:
            if not 0 <= index < count:
                raise MobiError("A Kindle record index is out of range.")
            start, end = offsets[index], offsets[index + 1]
            if end - start > cap:
                raise MobiError("A Kindle record exceeds the safety limit.")
            handle.seek(start)
            data = handle.read(end - start)
            if len(data) != end - start:
                raise MobiError("A Kindle record is truncated.")
            return data

        record0 = record(0, MAX_RECORD0_BYTES)
        compression = _u16(record0, 0)
        text_records = _u16(record0, 8)
        encryption = _u16(record0, 12)
        result = MobiText(compression={
            COMPRESSION_NONE: "none",
            COMPRESSION_PALMDOC: "palmdoc",
            COMPRESSION_HUFFCDIC: "huff/cdic",
        }.get(compression, str(compression)))

        encoding = "cp1252"
        extra_flags = 0
        huff_first = huff_count = 0
        if record0[16:20] == b"MOBI":
            header_length = _u32(record0, 20)
            if _u32(record0, 28) == 65001:
                encoding = "utf-8"
            version = _u32(record0, 36) if len(record0) >= 40 else 0
            if len(record0) >= 0x78:
                huff_first, huff_count = _u32(record0, 0x70), _u32(record0, 0x74)
            if version >= 5 and header_length >= 0xE4 and len(record0) >= 0xF4:
                extra_flags = _u16(record0, 0xF2)
            result.identifiers = _exth_identifiers(record0, 16 + header_length)

        if encryption != 0:
            result.encrypted = True
            result.notes.append(
                "This Kindle book is DRM-protected, so its text cannot be read; "
                "only its header metadata is available."
            )
            return result

        if compression == COMPRESSION_HUFFCDIC:
            if huff_count < 2 or huff_first + huff_count > count:
                raise MobiError("The HUFF/CDIC records are missing or out of range.")
            huff = HuffCdicReader(
                record(huff_first),
                [record(huff_first + i) for i in range(1, huff_count)],
            )
            decompress = huff.unpack
        elif compression == COMPRESSION_PALMDOC:
            decompress = palmdoc_decompress
        elif compression == COMPRESSION_NONE:
            def decompress(data: bytes) -> bytes:
                if len(data) > MAX_RECORD_OUTPUT:
                    raise MobiError("A Kindle text record exceeds the safety limit.")
                return data
        else:
            raise MobiError(f"Unsupported Kindle compression type {compression}.")

        byte_budget = max_chars * 4
        chunks: list[bytes] = []
        total = 0
        for index in range(1, min(text_records, count - 1) + 1):
            data = record(index)
            data = data[: len(data) - _trailing_size(data, extra_flags)]
            chunk = decompress(data)
            chunks.append(chunk)
            total += len(chunk)
            if total >= byte_budget:
                break

    result.text = _html_to_text(b"".join(chunks).decode(encoding, errors="replace"))[:max_chars]
    return result


def _exth_identifiers(record0: bytes, exth_start: int) -> list[str]:
    """ISBN (104) and ASIN (113) from EXTH, when the header carries them."""
    if record0[exth_start:exth_start + 4] != b"EXTH" or exth_start + 12 > len(record0):
        return []
    end = min(len(record0), exth_start + _u32(record0, exth_start + 4))
    pos = exth_start + 12
    identifiers: list[str] = []
    for _ in range(min(_u32(record0, exth_start + 8), 10_000)):
        if pos + 8 > end:
            break
        kind, length = _u32(record0, pos), _u32(record0, pos + 4)
        if length < 8 or pos + length > end:
            break
        value = record0[pos + 8:pos + length].decode("utf-8", errors="replace").strip()
        if kind == 104 and value:
            identifiers.append(f"isbn:{value}")
        elif kind == 113 and value:
            identifiers.append(f"asin:{value}")
        pos += length
    return identifiers

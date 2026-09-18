from __future__ import annotations

import subprocess
import zipfile

import app.archive_probe as archive_probe


def test_zip_crc_probe_runs_in_worker(tmp_path):
    path = tmp_path / "valid.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("chapter.txt", "hello world")

    payload = archive_probe.inspect_zip_crc(path)

    assert "error" not in payload
    assert payload["badMember"] is None


def test_zip_crc_probe_reports_corrupt_member(tmp_path):
    path = tmp_path / "corrupt.zip"
    payload_bytes = b"bookguard-crc-payload"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("chapter.txt", payload_bytes)

    raw = bytearray(path.read_bytes())
    offset = raw.index(payload_bytes)
    raw[offset] ^= 0x01
    path.write_bytes(raw)

    payload = archive_probe.inspect_zip_crc(path)

    assert "error" not in payload
    assert payload["badMember"] == "chapter.txt"


def test_zip_crc_probe_timeout_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "book.zip"
    path.write_bytes(b"PK")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=20)

    monkeypatch.setattr(archive_probe.subprocess, "run", fake_run)

    payload = archive_probe.inspect_zip_crc(path)

    assert payload == {"error": "ZIP CRC probe timed out."}

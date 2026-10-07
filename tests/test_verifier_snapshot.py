from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app import verifier


@dataclass
class _Extracted:
    metadata: dict
    text: str
    identifiers: list[str]
    notes: list[str]
    front_text: str
    source: str


def _result(path: Path) -> dict:
    return {
        "id": 1,
        "scan_id": "scan-1",
        "file_id": 2,
        "book_id": 3,
        "format": "ebook",
        "author": "Ann Patchett",
        "title": "Bel Canto",
        "local_path": str(path),
    }


def test_verifier_sends_security_and_identity_the_same_private_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(verifier, "bindery_series_context", lambda _id: [])
    source = tmp_path / "book.txt"
    source.write_text("Bel Canto by Ann Patchett", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(verifier.settings, "config_dir", str(config))
    monkeypatch.setattr(verifier.settings, "verification_snapshot_max_bytes", 1024 * 1024)

    observed = []

    def fake_security(path, **kwargs):
        observed.append(("security", str(path), Path(path).read_text(encoding="utf-8")))
        return {"safe": True, "message": "safe", "checks": {}, "failures": []}

    def fake_extract(path):
        observed.append(("identity", str(path), Path(path).read_text(encoding="utf-8")))
        return _Extracted(
            metadata={"title": "Bel Canto", "author": "Ann Patchett"},
            text="Bel Canto by Ann Patchett",
            identifiers=[],
            notes=[],
            front_text="Bel Canto by Ann Patchett",
            source="test",
        )

    monkeypatch.setattr(verifier, "inspect_ebook_security", fake_security)
    monkeypatch.setattr(verifier, "extract_ebook_identity", fake_extract)
    monkeypatch.setattr(
        verifier,
        "classify_identity",
        lambda *args: ("VERIFIED_CORRECT", 99, {}),
    )
    monkeypatch.setattr(
        verifier,
        "_verification_for_fingerprint",
        lambda *args: None,
    )
    monkeypatch.setattr(
        verifier,
        "_save_verification",
        lambda result, target, fingerprint, verdict, confidence, source_name, evidence: {
            "target": target,
            "fingerprint": fingerprint,
            "verdict": verdict,
            "evidence": evidence,
        },
    )

    result = verifier.verify_result(_result(source), force=True)

    assert result["verdict"] == "VERIFIED_CORRECT"
    assert observed[0][0] == "security"
    assert observed[1][0] == "identity"
    assert observed[0][1] == observed[1][1]
    assert observed[0][1] != str(source)
    assert observed[0][2] == observed[1][2] == "Bel Canto by Ann Patchett"
    assert Path(observed[0][1]).exists() is False


def test_verifier_fails_closed_if_source_changes_while_snapshot_is_parsed(tmp_path, monkeypatch):
    monkeypatch.setattr(verifier, "bindery_series_context", lambda _id: [])
    source = tmp_path / "book.txt"
    source.write_text("Bel Canto by Ann Patchett", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(verifier.settings, "config_dir", str(config))
    monkeypatch.setattr(verifier.settings, "verification_snapshot_max_bytes", 1024 * 1024)

    def fake_security(path, **kwargs):
        source.write_text("different bytes", encoding="utf-8")
        return {"safe": True, "message": "safe", "checks": {}, "failures": []}

    monkeypatch.setattr(verifier, "inspect_ebook_security", fake_security)
    monkeypatch.setattr(
        verifier,
        "extract_ebook_identity",
        lambda path: _Extracted(
            metadata={},
            text="",
            identifiers=[],
            notes=[],
            front_text="",
            source="test",
        ),
    )
    monkeypatch.setattr(
        verifier,
        "classify_identity",
        lambda *args: ("INSUFFICIENT_EVIDENCE", 0, {}),
    )
    monkeypatch.setattr(
        verifier,
        "_verification_for_fingerprint",
        lambda *args: None,
    )

    saved = {}

    def fake_save(result, target, fingerprint, verdict, confidence, source_name, evidence):
        saved.update(
            verdict=verdict,
            source=source_name,
            evidence=evidence,
        )
        return dict(saved)

    monkeypatch.setattr(verifier, "_save_verification", fake_save)

    result = verifier.verify_result(_result(source), force=True)

    assert result["verdict"] == "UNSAFE_FILE"
    assert result["source"] == "source-snapshot"
    assert result["evidence"]["security"]["checks"]["sourceStability"]["code"] == "changed"

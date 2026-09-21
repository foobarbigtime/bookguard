from __future__ import annotations

import json

from app.config import settings
from app.db import add_result, create_scan, finish_scan, init_local_db, local_conn
from app.history import operation_detail, operation_history
from app.triage import init_triage_db
from app.verifier import init_verification_db


def _seed(tmp_path):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    init_local_db()
    init_triage_db()
    init_verification_db()

    create_scan("history-scan", 1)
    add_result(
        "history-scan",
        {
            "file_id": 10,
            "book_id": 20,
            "author": "Ann Patchett",
            "title": "Bel Canto",
            "format": "ebook",
            "stored_path": "/data/media/books/Bel Canto.epub",
            "local_path": "/books/Bel Canto.epub",
            "classification": "REVIEW",
            "risk_score": 70,
            "reason_code": "MISMATCH",
            "reasons": ["history fixture"],
            "metadata": {},
        },
    )
    finish_scan("history-scan")
    with local_conn() as conn:
        result_id = int(
            conn.execute(
                "SELECT id FROM scan_results WHERE scan_id='history-scan'"
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO ebook_acquisitions(
                result_id, scan_id, book_id, candidate_guid, candidate_title,
                status, created_at, updated_at
            ) VALUES (?, 'history-scan', 20, 'guid', 'Bel Canto candidate',
                      'verified', '2026-09-20T12:00:00+00:00',
                      '2026-09-20T12:07:00+00:00')
            """,
            (result_id,),
        )
        conn.execute(
            """
            INSERT INTO ebook_admissions(
                result_id, scan_id, book_id, staged_relative_path, stored_path,
                local_path, status, created_at, updated_at
            ) VALUES (?, 'history-scan', 20, 'Bel Canto.epub',
                      '/data/media/books/Bel Canto.epub', '/books/Bel Canto.epub',
                      'registered', '2026-09-20T12:08:00+00:00',
                      '2026-09-20T12:09:00+00:00')
            """,
            (result_id,),
        )
        conn.execute(
            """
            INSERT INTO cleanup_actions(
                result_id, scan_id, file_id, book_id, classification, format,
                author, title, stored_path, local_path, action_kind, status,
                created_at, completed_at
            ) VALUES (?, 'history-scan', 10, 20, 'REVIEW', 'ebook',
                      'Ann Patchett', 'Bel Canto',
                      '/data/media/books/Bel Canto.epub', '/books/Bel Canto.epub',
                      'TRIAGE_QUARANTINE', 'applied',
                      '2026-09-20T12:10:00+00:00',
                      '2026-09-20T12:11:00+00:00')
            """,
            (result_id,),
        )
        conn.execute(
            """
            INSERT INTO metadata_repairs(
                result_id, scan_id, file_id, book_id, format, stored_path,
                local_path, repair_kind, status, before_json, after_json,
                created_at, completed_at
            ) VALUES (?, 'history-scan', 10, 20, 'ebook',
                      '/data/media/books/Bel Canto.epub', '/books/Bel Canto.epub',
                      'EPUB_METADATA', 'applied', '{}', '{}',
                      '2026-09-20T12:12:00+00:00',
                      '2026-09-20T12:13:00+00:00')
            """,
            (result_id,),
        )
        conn.execute(
            """
            INSERT INTO content_verifications(
                signature, result_id, scan_id, file_id, book_id, format, author,
                title, target_path, file_fingerprint, verdict, confidence,
                source, evidence_json, created_at, updated_at
            ) VALUES ('history-verification', ?, 'history-scan', 10, 20, 'ebook',
                      'Ann Patchett', 'Bel Canto', '/books/Bel Canto.epub',
                      'fingerprint', 'VERIFIED_CORRECT', 99, 'bookguard',
                      '{}', '2026-09-20T12:14:00+00:00',
                      '2026-09-20T12:15:00+00:00')
            """,
            (result_id,),
        )
        conn.execute(
            """
            INSERT INTO triage_decisions(
                signature, result_id, scan_id, file_id, book_id, classification,
                reason_code, format, author, title, stored_path, decision,
                created_at, updated_at
            ) VALUES ('history-triage', ?, 'history-scan', 10, 20, 'REVIEW',
                      'MISMATCH', 'ebook', 'Ann Patchett', 'Bel Canto',
                      '/data/media/books/Bel Canto.epub', 'KEEP',
                      '2026-09-20T12:16:00+00:00',
                      '2026-09-20T12:17:00+00:00')
            """,
            (result_id,),
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hardlink_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS hardlink_alias_cleanups (
                correction_id INTEGER PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT
            );
            """
        )
        snapshot = json.dumps({
            "source": "/staging/Bel Canto.epub",
            "wrong": {"title": "Wrong Book", "author": "Other Author"},
            "retained": {"title": "Bel Canto", "author": "Ann Patchett"},
        })
        cursor = conn.execute(
            """
            INSERT INTO hardlink_corrections(
                file_id, snapshot_json, status, created_at, completed_at
            ) VALUES (10, ?, 'applied',
                      '2026-09-20T12:18:00+00:00',
                      '2026-09-20T12:19:00+00:00')
            """,
            (snapshot,),
        )
        correction_id = int(cursor.lastrowid)
        conn.execute(
            """
            INSERT INTO hardlink_alias_cleanups(
                correction_id, snapshot_json, status, created_at, completed_at
            ) VALUES (?, ?, 'applied',
                      '2026-09-20T12:20:00+00:00',
                      '2026-09-20T12:21:00+00:00')
            """,
            (correction_id, snapshot),
        )
        conn.commit()
    return original


def test_operation_history_combines_durable_sources_newest_first(tmp_path):
    original = _seed(tmp_path)
    try:
        history = operation_history(100)
    finally:
        settings.config_dir = original

    kinds = {item["kind"] for item in history["items"]}
    assert {
        "acquisition",
        "admission",
        "cleanup",
        "repair",
        "verification",
        "triage",
        "hardlink_correction",
        "hardlink_cleanup",
    } <= kinds
    timestamps = [item["timestamp"] for item in history["items"]]
    assert timestamps == sorted(timestamps, reverse=True)
    assert history["items"][0]["kind"] == "hardlink_cleanup"
    quarantine = next(item for item in history["items"] if item["kind"] == "cleanup")
    assert quarantine["kindLabel"] == "Quarantine"
    assert quarantine["title"] == "Bel Canto"


def test_operation_history_limit_is_applied(tmp_path):
    original = _seed(tmp_path)
    try:
        history = operation_history(3)
    finally:
        settings.config_dir = original

    assert history["count"] == 3
    assert len(history["items"]) == 3


def test_operation_history_does_not_create_optional_tables(tmp_path):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    try:
        init_local_db()
        with local_conn() as conn:
            before = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

        operation_history()

        with local_conn() as conn:
            after = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
    finally:
        settings.config_dir = original

    assert after == before
    assert "hardlink_corrections" not in after
    assert "hardlink_alias_cleanups" not in after
    assert "content_verifications" not in after
    assert "triage_decisions" not in after


def test_operation_detail_reads_all_supported_durable_record_types(tmp_path):
    original = _seed(tmp_path)
    try:
        expected = {
            "acquisition": "verified",
            "admission": "registered",
            "cleanup": "applied",
            "repair": "applied",
            "verification": "verified_correct",
            "hardlink-correction": "applied",
            "hardlink-cleanup": "applied",
            "triage": "keep",
        }
        details = {
            kind: operation_detail(kind, 1)
            for kind in expected
        }
    finally:
        settings.config_dir = original

    for kind, status in expected.items():
        assert details[kind] is not None
        assert details[kind]["kind"] == kind
        assert details[kind]["status"] == status
        assert details[kind]["historyHref"] == "/history"

    assert details["acquisition"]["title"] == "Bel Canto"
    assert details["verification"]["title"] == "Bel Canto"
    assert details["hardlink-correction"]["path"] == "/staging/Bel Canto.epub"
    assert details["hardlink-cleanup"]["path"] == "/staging/Bel Canto.epub"
    assert details["hardlink-correction"]["title"] == "Bel Canto"
    assert details["hardlink-correction"]["author"] == "Ann Patchett"
    assert "Wrong Book" in details["hardlink-correction"]["summary"]
    assert "Bel Canto" in details["hardlink-correction"]["summary"]
    assert details["hardlink-correction"]["guidance"] is None


def test_operation_detail_decodes_stored_evidence(tmp_path):
    original = _seed(tmp_path)
    try:
        with local_conn() as conn:
            conn.execute(
                "UPDATE content_verifications SET evidence_json=? WHERE id=1",
                (json.dumps({"signature": "epub", "safe": True}),),
            )
            conn.execute(
                "UPDATE metadata_repairs SET before_json=?, after_json=? WHERE id=1",
                (
                    json.dumps({"title": "Old"}),
                    json.dumps({"title": "Bel Canto"}),
                ),
            )
            conn.commit()

        verification = operation_detail("verification", 1)
        repair = operation_detail("repair", 1)
    finally:
        settings.config_dir = original

    assert verification is not None
    assert verification["evidence"][0]["value"]["safe"] is True
    assert '"safe": true' in verification["evidence"][0]["pretty"]
    assert repair is not None
    assert [block["label"] for block in repair["evidence"]] == ["Before", "After"]


def test_acquisition_detail_omits_grab_provider_response(tmp_path):
    original = _seed(tmp_path)
    try:
        with local_conn() as conn:
            conn.execute(
                "UPDATE ebook_acquisitions SET grab_response_json=? WHERE id=1",
                (json.dumps({"downloadUrl": "https://provider.invalid/private"}),),
            )
            conn.commit()
        detail = operation_detail("acquisition", 1)
    finally:
        settings.config_dir = original

    assert detail is not None
    assert all(field["name"] != "grab_response_json" for field in detail["fields"])
    assert all(block["label"] != "Grab response" for block in detail["evidence"])
    assert detail["notes"]
    assert "intentionally omitted" in detail["notes"][0]


def test_operation_detail_unknown_or_missing_record_returns_none(tmp_path):
    original = _seed(tmp_path)
    try:
        assert operation_detail("not-a-kind", 1) is None
        assert operation_detail("verification", 999999) is None
    finally:
        settings.config_dir = original


def test_operation_detail_does_not_create_optional_tables(tmp_path):
    original = settings.config_dir
    settings.config_dir = str(tmp_path / "config")
    try:
        init_local_db()
        with local_conn() as conn:
            before = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

        assert operation_detail("hardlink-correction", 1) is None
        assert operation_detail("verification", 1) is None
        assert operation_detail("triage", 1) is None

        with local_conn() as conn:
            after = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
    finally:
        settings.config_dir = original

    assert after == before


def test_operation_detail_explains_review_state(tmp_path):
    original = _seed(tmp_path)
    try:
        with local_conn() as conn:
            conn.execute(
                """
                UPDATE content_verifications
                SET verdict='INSUFFICIENT_EVIDENCE'
                WHERE id=1
                """
            )
            conn.commit()
        detail = operation_detail("verification", 1)
    finally:
        settings.config_dir = original

    assert detail is not None
    assert detail["guidance"] is not None
    assert detail["guidance"]["label"] == "Verification evidence is insufficient"
    assert "manual review" in detail["guidance"]["nextStep"]



def test_observe_decision_appears_in_history_and_detail(tmp_path):
    original = _seed(tmp_path)
    try:
        with local_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO automation_observations(
                    signature, policy_version, mode, subject_kind, subject_id,
                    result_id, book_id, title, author, path, state, decision,
                    reason_code, reason, evidence_json, first_seen_at,
                    last_seen_at, observed_count
                ) VALUES (
                    'observe-history', '1', 'observe', 'result', '1',
                    1, 20, 'Bel Canto', 'Ann Patchett',
                    '/data/media/books/Bel Canto.epub',
                    'insufficient_evidence', 'attention',
                    'INSUFFICIENT_EVIDENCE',
                    'Not enough deterministic identity evidence.',
                    '{"nextStep":"Review verification evidence."}',
                    '2026-09-20T12:22:00+00:00',
                    '2026-09-20T12:22:00+00:00', 1
                )
                """
            )
            observe_id = int(cursor.lastrowid)
            conn.commit()

        history = operation_history(100)
        detail = operation_detail("observe", observe_id)
    finally:
        settings.config_dir = original

    observed = next(item for item in history["items"] if item["kind"] == "observe")
    assert observed["status"] == "attention"
    assert observed["detailHref"] == f"/history/observe/{observe_id}"

    assert detail is not None
    assert detail["title"] == "Bel Canto"
    assert detail["status"] == "attention"
    assert "did not authorize or perform" in detail["summary"]
    assert detail["guidance"]["nextStep"] == "Review verification evidence."



def test_verification_detail_builds_media_evidence_gui_summary(tmp_path):
    original = _seed(tmp_path)
    try:
        evidence = {
            "expected": {
                "title": "Bel Canto",
                "author": "Ann Patchett",
                "mediaKind": "audiobook",
            },
            "actualMedia": {
                "candidateCount": 2,
                "counts": {"audiobook": 2},
                "detected": [
                    {
                        "path": "/audio/01.mp3",
                        "kind": "audiobook",
                        "detectedFormat": "mp3",
                    },
                    {
                        "path": "/audio/02.mp3",
                        "kind": "audiobook",
                        "detectedFormat": "mp3",
                    },
                ],
            },
            "technical": {
                "verdict": "PASS",
                "reason_code": "AUDIO_TECHNICAL_PASS",
                "file_count": 2,
                "readable_file_count": 2,
                "total_duration_seconds": 7200.0,
                "chapter_count": 8,
                "codecs": ["mp3"],
            },
            "identity": {
                "detected_title": "Bel Canto",
                "detected_author": "Ann Patchett",
                "classification": "PASS",
                "reasonCode": "MATCH",
            },
            "reasonCode": "AUDIOBOOK_IDENTITY_VERIFIED",
            "explanation": "Audio identity is verified.",
        }
        with local_conn() as conn:
            conn.execute(
                """
                UPDATE content_verifications
                SET format='audiobook',
                    verdict='VERIFIED_CORRECT',
                    source='audiobook-evidence',
                    evidence_json=?
                WHERE id=1
                """,
                (json.dumps(evidence),),
            )
            conn.commit()

        detail = operation_detail("verification", 1)
    finally:
        settings.config_dir = original

    assert detail is not None
    summary = detail["verificationSummary"]
    assert summary["expectedKind"] == "audiobook"
    assert summary["detectedKind"] == "audiobook"
    assert summary["detectedFormats"] == ["mp3"]
    assert summary["fileCount"] == 2
    assert summary["readableFileCount"] == 2
    assert summary["durationSeconds"] == 7200.0
    assert summary["detectedTitle"] == "Bel Canto"
    assert summary["detectedAuthor"] == "Ann Patchett"
    assert summary["planLabel"] == "No repair required"

def test_verification_detail_surfaces_mixed_whole_set_and_filename_evidence(tmp_path):
    original = _seed(tmp_path)
    try:
        mixed_evidence = {
            "expected": {
                "title": "1st to Die",
                "author": "James Patterson",
                "mediaKind": "audiobook",
            },
            "actualMedia": {
                "candidateCount": 25,
                "counts": {"audiobook": 24, "unknown": 1},
            },
            "technical": {
                "verdict": "REVIEW",
                "file_count": 24,
                "readable_file_count": 24,
            },
            "identity": {
                "detected_title": "WMC 01 - 1st to Die",
                "detected_author": "James Patterson",
                "wholeSet": {
                    "readableCount": 24,
                    "titleMatchCount": 8,
                    "titleMismatchCount": 16,
                    "foreignPairCount": 0,
                    "distinctMismatchTitleCount": 6,
                    "mixedContent": True,
                    "topMismatchTitles": [
                        {"title": "WMC 02 - 2nd Chance", "count": 2},
                        {"title": "WMC 03 - 3rd Degree", "count": 2},
                    ],
                },
                "filenameSupport": {
                    "fileCount": 24,
                    "pairMatchCount": 8,
                    "requiredPairCount": 20,
                    "strong": False,
                    "examples": [],
                },
            },
            "reasonCode": "MIXED_AUDIO_CONTENT",
            "explanation": "Multiple embedded work identities were found.",
        }
        with local_conn() as conn:
            conn.execute(
                """
                UPDATE content_verifications
                SET format='audiobook',
                    title='1st to Die',
                    author='James Patterson',
                    verdict='WRONG_CONTENT',
                    confidence=98,
                    source='audiobook-whole-set-evidence',
                    evidence_json=?
                WHERE id=1
                """,
                (json.dumps(mixed_evidence),),
            )
            conn.commit()

        mixed = operation_detail("verification", 1)
        assert mixed is not None
        summary = mixed["verificationSummary"]
        assert summary["mixedContent"] is True
        assert summary["wholeSetReadableCount"] == 24
        assert summary["wholeSetTitleMatchCount"] == 8
        assert summary["wholeSetTitleMismatchCount"] == 16
        assert summary["topMismatchTitles"][0] == {
            "title": "WMC 02 - 2nd Chance",
            "count": 2,
        }

        filename_evidence = {
            "expected": {
                "title": "Finders Keepers",
                "author": "Stephen King",
                "mediaKind": "audiobook",
            },
            "actualMedia": {
                "candidateCount": 1,
                "counts": {"audiobook": 1},
            },
            "technical": {
                "verdict": "PASS",
                "file_count": 1,
                "readable_file_count": 1,
            },
            "identity": {
                "detected_title": "",
                "detected_author": "",
                "wholeSet": {
                    "readableCount": 1,
                    "titleMatchCount": 0,
                    "titleMismatchCount": 0,
                    "mixedContent": False,
                },
                "filenameSupport": {
                    "fileCount": 1,
                    "pairMatchCount": 1,
                    "requiredPairCount": 1,
                    "strong": True,
                    "examples": ["Stephen King - Finders Keepers (2015).mp3"],
                },
            },
            "reasonCode": "AUDIOBOOK_FILENAME_IDENTITY_VERIFIED",
            "explanation": "Leaf filename supports the expected identity.",
        }
        with local_conn() as conn:
            conn.execute(
                """
                UPDATE content_verifications
                SET title='Finders Keepers',
                    author='Stephen King',
                    verdict='VERIFIED_CORRECT',
                    confidence=90,
                    source='audiobook-filename-evidence',
                    evidence_json=?
                WHERE id=1
                """,
                (json.dumps(filename_evidence),),
            )
            conn.commit()

        filename = operation_detail("verification", 1)
    finally:
        settings.config_dir = original

    assert filename is not None
    summary = filename["verificationSummary"]
    assert summary["mixedContent"] is False
    assert summary["filenameStrong"] is True
    assert summary["filenamePairMatchCount"] == 1
    assert summary["filenameFileCount"] == 1
    assert summary["filenameExamples"] == ["Stephen King - Finders Keepers (2015).mp3"]


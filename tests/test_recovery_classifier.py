from __future__ import annotations

from app.recovery_classifier import (
    classify_acquisition_failure,
    classify_admission_failure,
)


def test_already_imported_acquisition_selects_alternate_candidate():
    classified = classify_acquisition_failure(
        "failed",
        'Bindery grab failed: Bindery POST /queue/grab returned HTTP 409: {"error":"already grabbed: this release has already been imported"}',
    )

    assert classified is not None
    assert classified.recoverable is True
    assert classified.reason_code == "ACQUISITION_RELEASE_ALREADY_IMPORTED"
    assert classified.plan_kind == "SELECT_ALTERNATE_REPLACEMENT"
    assert classified.retry_same_operation is False
    assert classified.max_retries == 0


def test_transient_acquisition_has_bounded_backoff():
    classified = classify_acquisition_failure(
        "failed",
        "Bindery POST /queue/grab returned HTTP 503: temporarily unavailable",
    )

    assert classified is not None
    assert classified.reason_code == "ACQUISITION_TRANSIENT_BINDERY_FAILURE"
    assert classified.retry_same_operation is True
    assert classified.max_retries == 3
    assert classified.backoff_seconds == (30, 120, 300)


def test_admission_errno22_before_publication_requires_capability_recovery():
    classified = classify_admission_failure(
        "failed",
        "[Errno 22] Invalid argument: PosixPath('/admission-books/Stephen King/Sometimes They Come Back (1974)/Sometimes They Come Back - Stephen King.epub')",
        None,
    )

    assert classified is not None
    assert classified.reason_code == "ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED"
    assert classified.plan_kind == "RECOVER_ADMISSION_PUBLICATION"
    assert classified.retry_same_operation is False
    assert classified.max_retries == 0


def test_unclassified_failure_remains_attention():
    assert classify_acquisition_failure(
        "failed",
        "A completely unknown acquisition failure",
    ) is None
    assert classify_admission_failure(
        "failed",
        "A completely unknown admission failure",
        None,
    ) is None

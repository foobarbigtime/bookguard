from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class RecoveryClassification:
    recoverable: bool
    reason_code: str
    plan_kind: str
    explanation: str
    retry_same_operation: bool = False
    max_retries: int = 0
    backoff_seconds: tuple[int, ...] = ()


_HTTP_STATUS_RE = re.compile(r"\bHTTP\s+(\d{3})\b", re.IGNORECASE)


def _http_status(error: str) -> int | None:
    match = _HTTP_STATUS_RE.search(error or "")
    return int(match.group(1)) if match else None


def classify_acquisition_failure(status: str, error: str) -> RecoveryClassification | None:
    if str(status or "").casefold() != "failed":
        return None

    text = str(error or "").strip()
    folded = text.casefold()
    status_code = _http_status(text)

    if (
        status_code == 409
        and "already grabbed" in folded
        and ("already been imported" in folded or "already imported" in folded)
    ):
        return RecoveryClassification(
            recoverable=True,
            reason_code="ACQUISITION_RELEASE_ALREADY_IMPORTED",
            plan_kind="SELECT_ALTERNATE_REPLACEMENT",
            explanation=(
                "Bindery rejected the selected release because that exact release was "
                "already grabbed/imported. Retrying the same candidate would repeat a "
                "known-bad transition; recovery should re-search and choose a different "
                "candidate under the normal safety policy."
            ),
            retry_same_operation=False,
            max_retries=0,
        )

    transient_statuses = {429, 500, 502, 503, 504}
    transient_markers = (
        "timed out",
        "timeout",
        "connection reset",
        "connection refused",
        "temporarily unavailable",
    )
    if status_code in transient_statuses or any(marker in folded for marker in transient_markers):
        return RecoveryClassification(
            recoverable=True,
            reason_code="ACQUISITION_TRANSIENT_BINDERY_FAILURE",
            plan_kind="RETRY_ACQUISITION_TRANSIENT",
            explanation=(
                "The acquisition failed with a transient Bindery/transport condition. "
                "The same transition may be retried only with bounded backoff after "
                "fresh readiness and candidate validation."
            ),
            retry_same_operation=True,
            max_retries=3,
            backoff_seconds=(30, 120, 300),
        )

    return None


def classify_admission_failure(
    status: str,
    error: str,
    publication_method: str | None = None,
) -> RecoveryClassification | None:
    if str(status or "").casefold() != "failed":
        return None

    text = str(error or "").strip()
    folded = text.casefold()
    status_code = _http_status(text)

    if (
        ("[errno 22]" in folded or "invalid argument" in folded)
        and not str(publication_method or "").strip()
    ):
        return RecoveryClassification(
            recoverable=True,
            reason_code="ADMISSION_PUBLICATION_PRIMITIVE_UNSUPPORTED",
            plan_kind="RECOVER_ADMISSION_PUBLICATION",
            explanation=(
                "Admission failed before publication completed because the configured "
                "filesystem rejected a no-replace publication primitive. Recovery must "
                "revalidate the same staged bytes and destination, then prove a supported "
                "no-overwrite publication method before retrying."
            ),
            retry_same_operation=False,
            max_retries=0,
        )

    if status_code in {429, 500, 502, 503, 504} or any(
        marker in folded
        for marker in (
            "timed out",
            "timeout",
            "connection reset",
            "connection refused",
            "temporarily unavailable",
        )
    ):
        return RecoveryClassification(
            recoverable=True,
            reason_code="ADMISSION_TRANSIENT_BINDERY_FAILURE",
            plan_kind="RETRY_ADMISSION_TRANSIENT",
            explanation=(
                "Admission failed because a transient Bindery/transport dependency was "
                "unavailable. Recovery may retry only with bounded backoff after the "
                "staged bytes, destination, book identity, and readiness gates are "
                "revalidated."
            ),
            retry_same_operation=True,
            max_retries=3,
            backoff_seconds=(30, 120, 300),
        )

    return None

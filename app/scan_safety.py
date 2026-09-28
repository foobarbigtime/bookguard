"""Durable one-shot scan claim and scope checks for automatic registration."""

from __future__ import annotations

from pathlib import Path

from .admission import AdmissionSafetyError
from .config import load_automation_settings
from .db import local_conn, utc_now


def require_quiescent_admissions(target_id: int) -> None:
    """Refuse a global Bindery scan while another known admission is unresolved."""
    configured = load_automation_settings()
    root = Path(configured.admission_root)
    if not root.is_dir() or root.is_symlink():
        raise AdmissionSafetyError("The admission root is unavailable or symlinked.")
    root = root.resolve(strict=True)
    bindery_root = Path(configured.admission_bindery_root)
    with local_conn() as conn:
        target = conn.execute(
            "SELECT status, stored_path, publication_method FROM ebook_admissions WHERE id=?",
            (int(target_id),),
        ).fetchone()
        rows = conn.execute(
            """SELECT id, status, publication_method, failure_stage, stored_path
               FROM ebook_admissions WHERE id<>?""", (int(target_id),),
        ).fetchall()
    for row in rows:
        if row["status"] == "registered":
            continue
        if (
            row["status"] == "retired_before_publication"
            and row["failure_stage"] == "before_publication"
            and not row["publication_method"]
            and target is not None
            and target["status"] == "published"
            and target["publication_method"]
            and target["stored_path"] == row["stored_path"]
        ):
            # The target's separately proven publication now occupies this path.
            continue
        if row["publication_method"]:
            raise AdmissionSafetyError(
                f"Another unresolved admission #{row['id']} has published bytes."
            )
        try:
            relative = Path(str(row["stored_path"] or "")).relative_to(bindery_root)
        except ValueError as exc:
            raise AdmissionSafetyError(
                f"Admission #{row['id']} has an unproven destination mapping."
            ) from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise AdmissionSafetyError(
                f"Admission #{row['id']} has an unsafe destination mapping."
            )
        destination = root
        for part in relative.parts:
            destination /= part
            if destination.is_symlink():
                raise AdmissionSafetyError(
                    f"Admission #{row['id']} has a symlinked destination."
                )
        if destination.exists():
            raise AdmissionSafetyError(
                f"Another unresolved admission #{row['id']} has destination bytes."
            )


def transition_scan_status(
    *, admission_id: int, result_id: int, book_id: int, stored_path: str,
    sha256: str, publication_method: str, from_status: str, to_status: str,
) -> None:
    """Atomically claim the scan before POST, then record its confirmed request."""
    if (from_status, to_status) not in {
        ("published", "scan_requesting"),
        ("scan_requesting", "scan_requested"),
    }:
        raise AdmissionSafetyError("Unsupported scan status transition.")
    with local_conn() as conn:
        changed = conn.execute(
            """UPDATE ebook_admissions
               SET status=?, error=NULL, updated_at=?
               WHERE id=? AND status=? AND result_id=? AND book_id=?
                 AND stored_path=? AND staged_sha256=? AND publication_method=?""",
            (
                to_status, utc_now(), int(admission_id), from_status,
                int(result_id), int(book_id), str(stored_path),
                str(sha256), str(publication_method),
            ),
        ).rowcount
        if changed != 1:
            raise AdmissionSafetyError("Admission changed at the one-shot scan boundary.")
        conn.commit()

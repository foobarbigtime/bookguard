from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import time
from typing import Any

from .bindery_client import BinderyClient, BinderyClientError
from .config import ConfigurationError, load_automation_settings, settings
from .db import (
    associations_inside_path,
    create_ebook_admission,
    ebook_acquisition_by_admission_id,
    ebook_admission_by_id,
    recent_ebook_admissions,
    result_by_id,
    update_ebook_admission,
)
from .file_safety import is_within, sha256_file
from .staging import (
    MIN_ADMISSION_CONFIDENCE,
    StagingSafetyError,
    book_identity,
    resolve_staged_file,
    verify_ebook_file,
)


class AdmissionSafetyError(RuntimeError):
    pass


class PublishedSnapshotError(RuntimeError):
    """Report a post-publication failure without losing the durable recovery state."""

    def __init__(self, publication_method: str, cause: OSError) -> None:
        self.publication_method = publication_method
        super().__init__(
            f"Snapshot was published using {publication_method}, "
            f"but publication finalization failed: {cause}"
        )


_admission_lock = threading.Lock()
_RECONCILABLE_STATUSES = {
    "verified",
    "published",
    "scan_requested",
    "registration_conflict",
    "registered",
}
_REGISTRATION_CORRECTION_STATUSES = {
    "registration_conflict",
    "registration_correcting",
}
_HISTORICAL_QUEUE_STATUSES = {
    "cancelled",
    "failed",
    "importblocked",
    "imported",
    "removed",
}
_REGISTRATION_CORRECTION_POLL_ATTEMPTS = 120
_REGISTRATION_CORRECTION_POLL_SECONDS = 0.5
_BINDERY_SETTING_DEFAULTS = {
    "import.mode": "auto",
    "import.drop_folder": "",
    "import.drop_layout": "flat",
    "import.drop_link_mode": "copy",
}


def _automation_settings():
    try:
        return load_automation_settings()
    except ConfigurationError as exc:
        raise AdmissionSafetyError(str(exc)) from exc


def _setting_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("value") or "").strip()
    return str(value or "").strip()


def _bindery_setting(client: BinderyClient, key: str) -> str:
    try:
        return _setting_text(client.get_setting(key))
    except BinderyClientError as exc:
        if "HTTP 404" in str(exc) and key in _BINDERY_SETTING_DEFAULTS:
            return _BINDERY_SETTING_DEFAULTS[key]
        raise


def admission_readiness(client: BinderyClient | None = None) -> dict[str, Any]:
    """Return the independent, fail-closed gates for direct ebook admission."""
    configured = _automation_settings()
    client = client or BinderyClient()
    try:
        import_mode = _bindery_setting(client, "import.mode").lower() or "auto"
        drop_folder = _bindery_setting(client, "import.drop_folder")
        drop_layout = _bindery_setting(client, "import.drop_layout").lower() or "flat"
        drop_link_mode = (
            _bindery_setting(client, "import.drop_link_mode").lower() or "copy"
        )
    except BinderyClientError as exc:
        raise AdmissionSafetyError(str(exc)) from exc

    admission_root = Path(configured.admission_root)
    staging_root = Path(configured.staging_root)
    quarantine_root = Path(settings.quarantine_root).resolve()
    admission_exists = admission_root.is_dir() and not admission_root.is_symlink()
    staging_exists = staging_root.is_dir() and not staging_root.is_symlink()
    resolved_admission = (
        admission_root.resolve() if admission_exists else admission_root.absolute()
    )
    resolved_staging = staging_root.resolve() if staging_exists else staging_root.absolute()
    expected_bindery_root = settings.ebook_bindery_prefix.rstrip("/")

    checks = {
        "actionsEnabled": settings.allow_actions,
        "admissionEnabled": configured.admission_enabled,
        "binderyExternalImport": import_mode == "external",
        "binderyDropFolderConfigured": bool(drop_folder),
        "dropFolderMappingConfirmed": bool(
            configured.bindery_drop_folder
            and drop_folder == configured.bindery_drop_folder
        ),
        "supportedDropLayout": drop_layout in {"flat", "templated"},
        "supportedDropLinkMode": drop_link_mode in {"copy", "hardlink"},
        "stagingRootExists": staging_exists,
        "stagingRootWritable": staging_exists
        and os.access(resolved_staging, os.W_OK | os.X_OK),
        "admissionRootExists": admission_exists,
        "admissionRootWritable": admission_exists
        and os.access(resolved_admission, os.W_OK | os.X_OK),
        "binderyRootMappingConfirmed": bool(
            configured.admission_bindery_root
            and configured.admission_bindery_root == expected_bindery_root
        ),
        "stagingOutsideAdmissionRoot": not (
            is_within(resolved_staging, resolved_admission)
            or is_within(resolved_admission, resolved_staging)
        ),
        "quarantineOutsideAdmissionRoot": not (
            is_within(quarantine_root, resolved_admission)
            or is_within(resolved_admission, quarantine_root)
        ),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    ready = not blockers
    return {
        "ready": ready,
        "checks": checks,
        "blockers": blockers,
        "admissionRoot": str(resolved_admission),
        "binderyLibraryRoot": configured.admission_bindery_root,
        "binderyImportMode": import_mode,
        "binderyDropFolder": drop_folder,
        "message": (
            "Direct ebook admission is ready for an explicitly confirmed operation."
            if ready
            else "Direct ebook admission is blocked until every safety check passes."
        ),
    }


def _relative_target(result: dict[str, Any]) -> Path:
    stored = Path(str(result.get("stored_path") or ""))
    local = Path(str(result.get("local_path") or ""))
    stored_root = Path(settings.ebook_bindery_prefix)
    local_root = Path(settings.ebook_root)
    if not stored.is_absolute() or not local.is_absolute():
        raise AdmissionSafetyError("Former ebook paths must be absolute.")
    try:
        stored_relative = stored.relative_to(stored_root)
        local_relative = local.relative_to(local_root)
    except ValueError as exc:
        raise AdmissionSafetyError(
            "The former ebook path is outside the configured Bindery/BookGuard roots."
        ) from exc
    if stored_relative != local_relative or not stored_relative.parts:
        raise AdmissionSafetyError(
            "The former ebook path does not map consistently across roots."
        )
    if any(part in {"", ".", ".."} for part in stored_relative.parts):
        raise AdmissionSafetyError("The former ebook path contains an unsafe component.")
    return stored_relative


def _destination_for_result(
    result: dict[str, Any],
    staged_path: Path,
) -> tuple[Path, str]:
    configured = _automation_settings()
    admission_root = Path(configured.admission_root).resolve(strict=True)
    relative = _relative_target(result)
    destination = admission_root / relative
    if destination.suffix.lower() != staged_path.suffix.lower():
        raise AdmissionSafetyError(
            "The staged format differs from the former library path; automatic renaming is refused."
        )

    cursor = admission_root
    for part in relative.parent.parts:
        cursor /= part
        if cursor.is_symlink():
            raise AdmissionSafetyError("Symlinked destination directories are not accepted.")
    parent = destination.parent
    if not parent.is_dir():
        raise AdmissionSafetyError(
            "The former library directory is missing; create or review it manually."
        )
    try:
        parent.resolve(strict=True).relative_to(admission_root)
    except (RuntimeError, ValueError) as exc:
        raise AdmissionSafetyError("The destination resolves outside the admission root.") from exc
    if destination.exists() or destination.is_symlink():
        raise AdmissionSafetyError(
            "The destination already exists; BookGuard will not overwrite it."
        )
    bindery_path = str(Path(configured.admission_bindery_root) / relative)
    return destination, bindery_path


def _book_has_ebook(book: dict[str, Any]) -> bool:
    if str(book.get("ebookFilePath") or "").strip():
        return True
    return any(
        str(item.get("format") or "").lower() == "ebook"
        for item in (book.get("bookFiles") or [])
        if isinstance(item, dict)
    )


def _book_has_exact_ebook(book: dict[str, Any], stored_path: str) -> bool:
    expected = os.path.normpath(stored_path)
    return any(
        isinstance(item, dict)
        and str(item.get("format") or "").casefold() == "ebook"
        and os.path.normpath(str(item.get("path") or "")) == expected
        for item in (book.get("bookFiles") or [])
    )


def _exact_ebook_associations(stored_path: str) -> list[dict[str, Any]]:
    expected = os.path.normpath(stored_path)
    return [
        item
        for item in associations_inside_path(stored_path)
        if str(item.get("format") or "").casefold() == "ebook"
        and os.path.normpath(str(item.get("stored_path") or "")) == expected
    ]


def _complete_queue_items(client: BinderyClient) -> list[dict[str, Any]]:
    try:
        payload = client.list_queue()
    except BinderyClientError as exc:
        raise AdmissionSafetyError(str(exc)) from exc
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise AdmissionSafetyError("Bindery returned an invalid queue response.")
    if payload.get("partial"):
        raise AdmissionSafetyError(
            "Bindery returned a partial queue response; correction stopped."
        )
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise AdmissionSafetyError(
            "Bindery queue response did not contain an item list."
        )
    return [item for item in raw_items if isinstance(item, dict)]


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _copy_stable_snapshot(
    source: Path,
    destination_dir: Path,
    max_bytes: int,
) -> tuple[Path, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    temp_path: Path | None = None
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode):
            raise AdmissionSafetyError("The staged source is no longer a regular file.")
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise AdmissionSafetyError("The staged source size is outside the admission limit.")

        private_dir = Path(
            tempfile.mkdtemp(
                prefix=".bookguard-admission-",
                dir=destination_dir,
            )
        )
        temp_path = private_dir / f"snapshot{source.suffix.lower()}"
        temp_fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        digest = hashlib.sha256()
        copied = 0
        with os.fdopen(temp_fd, "wb", closefd=True) as target, os.fdopen(
            os.dup(source_fd), "rb", closefd=True
        ) as staged:
            while chunk := staged.read(1024 * 1024):
                copied += len(chunk)
                if copied > max_bytes:
                    raise AdmissionSafetyError(
                        "The staged source grew beyond the admission size limit."
                    )
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fchmod(target.fileno(), 0o600)
            os.fsync(target.fileno())

        after_copy = os.fstat(source_fd)
        os.lseek(source_fd, 0, os.SEEK_SET)
        current_digest = hashlib.sha256()
        with os.fdopen(os.dup(source_fd), "rb", closefd=True) as staged:
            while chunk := staged.read(1024 * 1024):
                current_digest.update(chunk)
        after_hash = os.fstat(source_fd)
        current_path = source.stat()
        unchanged = (
            _same_file_state(before, after_copy)
            and _same_file_state(before, after_hash)
            and _same_file_state(before, current_path)
        )
        if not unchanged or current_digest.hexdigest() != digest.hexdigest():
            raise AdmissionSafetyError("The staged file changed while its snapshot was copied.")
        return temp_path, digest.hexdigest()
    except Exception:
        _cleanup_private_snapshot(temp_path)
        raise
    finally:
        os.close(source_fd)


def _rename_no_replace(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
    destination: Path,
) -> bool:
    """Rename without replacement, or report that the filesystem lacks support."""
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_directory_fd,
        os.fsencode(source_name),
        destination_directory_fd,
        os.fsencode(destination_name),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        return True

    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination,
        )
    unsupported_errors = {
        errno.EINVAL,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        errno.ENOTSUP,
    }
    if error_number in unsupported_errors:
        return False
    raise OSError(error_number, os.strerror(error_number), destination)


def _publish_no_replace(temp_path: Path, destination: Path) -> str:
    """Atomically publish a private snapshot without replacing any path."""
    source_directory_fd = os.open(
        temp_path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    destination_directory_fd = os.open(
        destination.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    published = False
    publication_method = ""
    try:
        renamed = _rename_no_replace(
            source_directory_fd,
            temp_path.name,
            destination_directory_fd,
            destination.name,
            destination,
        )
        if renamed:
            publication_method = "renameat2"
        else:
            os.link(
                temp_path.name,
                destination.name,
                src_dir_fd=source_directory_fd,
                dst_dir_fd=destination_directory_fd,
                follow_symlinks=False,
            )
            publication_method = "private-snapshot-link"

        published = True
        os.fsync(destination_directory_fd)
        if not renamed:
            os.unlink(temp_path.name, dir_fd=source_directory_fd)
            os.fsync(source_directory_fd)
    except OSError as exc:
        if published:
            raise PublishedSnapshotError(publication_method, exc) from exc
        raise
    finally:
        os.close(source_directory_fd)
        os.close(destination_directory_fd)
    _cleanup_private_snapshot(temp_path)
    return publication_method


def _cleanup_private_snapshot(temp_path: Path | None) -> None:
    if temp_path is None:
        return
    try:
        temp_path.unlink(missing_ok=True)
    except OSError:
        return
    parent = temp_path.parent
    if parent.name.startswith(".bookguard-admission-"):
        try:
            parent.rmdir()
        except OSError:
            pass


def _seal_snapshot(temp_path: Path, expected_hash: str) -> None:
    """Make the verified snapshot immutable to non-owner users before publish."""
    descriptor = os.open(temp_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if sha256_file(temp_path) != expected_hash:
        raise AdmissionSafetyError("The private snapshot changed while it was sealed.")


def _identity_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return int(before.get("id") or 0) == int(after.get("id") or 0) and book_identity(
        before
    ) == book_identity(after)


def _result_matches_book(result: dict[str, Any], book: dict[str, Any]) -> bool:
    title, author = book_identity(book)
    return (
        str(result.get("title") or "").strip().casefold() == title.casefold()
        and str(result.get("author") or "").strip().casefold() == author.casefold()
    )


def admit_staged_ebook(
    result: dict[str, Any],
    relative_path: str,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Verify, durably record, and atomically publish one staged ebook."""
    if not _admission_lock.acquire(blocking=False):
        raise AdmissionSafetyError("Another admission operation is already running.")
    admission_id: int | None = None
    temp_path: Path | None = None
    try:
        readiness = admission_readiness(client)
        if not readiness["ready"]:
            raise AdmissionSafetyError(
                "Admission readiness failed: " + ", ".join(readiness["blockers"])
            )
        if str(result.get("format") or "") != "ebook":
            raise AdmissionSafetyError("Direct admission currently supports ebooks only.")

        staging_root, staged_path = resolve_staged_file(relative_path)
        destination, expected_stored_path = _destination_for_result(result, staged_path)
        if expected_stored_path != str(result.get("stored_path") or ""):
            raise AdmissionSafetyError("The Bindery destination mapping changed since the scan.")

        client = client or BinderyClient()
        book_before = client.get_book(int(result["book_id"]))
        if not _result_matches_book(result, book_before):
            raise AdmissionSafetyError(
                "The historical result no longer matches Bindery's book identity."
            )
        if _book_has_ebook(book_before):
            raise AdmissionSafetyError("Bindery already tracks an ebook for this book.")

        staged_relative = staged_path.relative_to(staging_root).as_posix()
        admission_id = create_ebook_admission(result, staged_relative)
        temp_path, snapshot_hash = _copy_stable_snapshot(
            staged_path,
            destination.parent,
            _automation_settings().max_staged_ebook_bytes,
        )
        if sha256_file(temp_path) != snapshot_hash:
            raise AdmissionSafetyError(
                "The private snapshot checksum does not match its copied bytes."
            )

        verification = verify_ebook_file(
            int(result["book_id"]),
            temp_path,
            display_path=staged_relative,
            book=book_before,
        )
        if not verification["safeToAdmit"]:
            blockers = ", ".join(verification["admissionBlockers"])
            raise AdmissionSafetyError(
                f"Copied snapshot failed admission verification: {blockers}"
            )
        if int(verification["confidence"]) < MIN_ADMISSION_CONFIDENCE:
            raise AdmissionSafetyError(
                "Copied snapshot confidence is below the admission threshold."
            )
        if verification["sha256"] != snapshot_hash:
            raise AdmissionSafetyError("Copied snapshot changed before publication.")

        book_after = client.get_book(int(result["book_id"]))
        if not _identity_unchanged(book_before, book_after) or _book_has_ebook(book_after):
            raise AdmissionSafetyError(
                "Bindery's book identity or ebook state changed during admission."
            )
        if destination.exists() or destination.is_symlink():
            raise AdmissionSafetyError(
                "The destination appeared during verification; overwrite refused."
            )

        update_ebook_admission(
            admission_id,
            "verified",
            staged_sha256=snapshot_hash,
            verification=verification,
        )
        _seal_snapshot(temp_path, snapshot_hash)
        publication_method = _publish_no_replace(temp_path, destination)
        temp_path = None
        update_ebook_admission(
            admission_id,
            "published",
            publication_method=publication_method,
        )

        scan_state = "requested"
        scan_warning = ""
        try:
            client.scan_library()
            update_ebook_admission(admission_id, "scan_requested")
        except BinderyClientError as exc:
            if "HTTP 409" in str(exc):
                scan_state = "already_running"
                update_ebook_admission(admission_id, "scan_requested")
            else:
                scan_state = "request_failed"
                scan_warning = str(exc)
                update_ebook_admission(
                    admission_id,
                    "published",
                    error=scan_warning,
                )

        response_status = "published" if scan_state == "request_failed" else "scan_requested"
        return {
            "ok": True,
            "admissionId": admission_id,
            "bookId": int(result["book_id"]),
            "relativePath": staged_relative,
            "sha256": snapshot_hash,
            "libraryPath": str(destination),
            "binderyPath": expected_stored_path,
            "publicationMethod": publication_method,
            "status": response_status,
            "binderyScan": scan_state,
            "warning": scan_warning,
            "stagingRetained": True,
            "message": (
                "Verified bytes were atomically published without overwrite. "
                "The staged source is retained until Bindery registration is confirmed."
            ),
        }
    except PublishedSnapshotError as exc:
        if admission_id is not None:
            update_ebook_admission(
                admission_id,
                "published",
                publication_method=exc.publication_method,
                error=str(exc),
            )
        raise AdmissionSafetyError(str(exc)) from exc
    except (OSError, StagingSafetyError, BinderyClientError) as exc:
        error = str(exc)
        if admission_id is not None:
            update_ebook_admission(admission_id, "failed", error=error)
        raise AdmissionSafetyError(error) from exc
    except AdmissionSafetyError as exc:
        if admission_id is not None:
            update_ebook_admission(admission_id, "failed", error=str(exc))
        raise
    finally:
        _cleanup_private_snapshot(temp_path)
        _admission_lock.release()


def _verified_published_destination(
    admission: dict[str, Any],
    configured: Any,
) -> Path:
    try:
        relative = Path(admission["stored_path"]).relative_to(
            Path(configured.admission_bindery_root)
        )
    except ValueError as exc:
        raise AdmissionSafetyError(
            "The recorded admission path is outside the configured Bindery root."
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise AdmissionSafetyError(
            "The recorded admission path contains an unsafe component."
        )
    configured_root = Path(configured.admission_root)
    if configured_root.is_symlink():
        raise AdmissionSafetyError("The admission root must not be a symlink.")
    try:
        admission_root = configured_root.resolve(strict=True)
    except OSError as exc:
        raise AdmissionSafetyError("The configured admission root is unavailable.") from exc
    cursor = admission_root
    for part in relative.parent.parts:
        cursor /= part
        if cursor.is_symlink():
            raise AdmissionSafetyError("Symlinked destination directories are not accepted.")
    destination = admission_root / relative
    try:
        destination.parent.resolve(strict=True).relative_to(admission_root)
    except (RuntimeError, ValueError) as exc:
        raise AdmissionSafetyError(
            "The recorded destination resolves outside admission."
        ) from exc
    if not destination.is_file() or destination.is_symlink():
        raise AdmissionSafetyError("The published library file is missing or symlinked.")
    try:
        destination_hash = sha256_file(destination)
    except OSError as exc:
        raise AdmissionSafetyError(
            "The published library file could not be read."
        ) from exc
    if destination_hash != admission["staged_sha256"]:
        raise AdmissionSafetyError(
            "The published library file no longer matches the verified snapshot."
        )
    return destination


def _verified_staged_source(admission: dict[str, Any]) -> Path:
    try:
        _, staged_path = resolve_staged_file(str(admission["staged_relative_path"]))
    except StagingSafetyError as exc:
        raise AdmissionSafetyError(str(exc)) from exc
    try:
        staged_hash = sha256_file(staged_path)
    except OSError as exc:
        raise AdmissionSafetyError("The verified staged file could not be read.") from exc
    if staged_hash != admission["staged_sha256"]:
        raise AdmissionSafetyError(
            "The staged file no longer matches the verified snapshot."
        )
    return staged_path


def reconcile_admission(
    admission_id: int,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Confirm Bindery registered the published bytes; never delete staging."""
    configured = _automation_settings()
    if not settings.allow_actions:
        raise AdmissionSafetyError("Actions are disabled; reconciliation is blocked.")
    if not configured.admission_enabled:
        raise AdmissionSafetyError("Direct admission is disabled; reconciliation is blocked.")

    admission = ebook_admission_by_id(admission_id)
    if not admission:
        raise AdmissionSafetyError("Admission record not found.")
    admission_status = str(admission.get("status") or "")
    if admission_status not in _RECONCILABLE_STATUSES:
        raise AdmissionSafetyError(
            f"Admission status '{admission_status}' is not eligible for reconciliation."
        )
    if not admission.get("staged_sha256"):
        raise AdmissionSafetyError("The admission has no verified snapshot to reconcile.")

    _verified_published_destination(admission, configured)

    client = client or BinderyClient()
    try:
        book = client.get_book(int(admission["book_id"]))
    except BinderyClientError as exc:
        raise AdmissionSafetyError(str(exc)) from exc
    registered = any(
        str(item.get("format") or "").lower() == "ebook"
        and os.path.normpath(str(item.get("path") or ""))
        == os.path.normpath(str(admission["stored_path"]))
        for item in (book.get("bookFiles") or [])
        if isinstance(item, dict)
    )
    if registered:
        update_ebook_admission(admission_id, "registered")
    else:
        expected_book_id = int(admission["book_id"])
        try:
            exact_associations = _exact_ebook_associations(
                str(admission["stored_path"])
            )
        except sqlite3.Error as exc:
            raise AdmissionSafetyError(
                "Bindery path ownership could not be confirmed; no library scan "
                "was requested."
            ) from exc

        conflicting_associations = [
            item
            for item in exact_associations
            if int(item.get("book_id") or 0) != expected_book_id
        ]
        if conflicting_associations:
            owners = ", ".join(
                f"book #{int(item.get('book_id') or 0)} "
                f"({str(item.get('title') or 'unknown title')})"
                for item in conflicting_associations
            )
            error = (
                "Bindery registered the admitted ebook path to the wrong book: "
                f"{owners}. No additional library scan was requested. Correct "
                "the exact Bindery association, then explicitly recheck registration."
            )
            update_ebook_admission(
                admission_id,
                "registration_conflict",
                error=error,
            )
            return {
                "admissionId": admission_id,
                "bookId": expected_book_id,
                "registered": False,
                "status": "registration_conflict",
                "scanRequested": False,
                "registrationConflict": {
                    "expectedBookId": expected_book_id,
                    "storedPath": str(admission["stored_path"]),
                    "associations": conflicting_associations,
                },
                "stagingRetained": True,
                "message": error,
            }
        try:
            client.scan_library()
        except BinderyClientError as exc:
            if "HTTP 409" not in str(exc):
                raise AdmissionSafetyError(str(exc)) from exc
        update_ebook_admission(admission_id, "scan_requested")

    return {
        "admissionId": admission_id,
        "bookId": int(admission["book_id"]),
        "registered": registered,
        "status": "registered" if registered else "scan_requested",
        "scanRequested": not registered,
        "registrationConflict": None,
        "stagingRetained": True,
        "message": (
            "Bindery registration is confirmed; the staged source remains available for manual cleanup."
            if registered
            else "Bindery has not registered the file yet; a library scan was requested."
        ),
    }


def correct_registration_conflict(
    admission_id: int,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Explicitly reassign one proven wrong Bindery owner without changing bytes."""
    if not _admission_lock.acquire(blocking=False):
        raise AdmissionSafetyError("Another admission operation is already running.")

    correction_started = False
    try:
        configured = _automation_settings()
        if not settings.allow_actions:
            raise AdmissionSafetyError(
                "Actions are disabled; registration correction is blocked."
            )
        if not configured.automatic_reacquisition:
            raise AdmissionSafetyError(
                "Automatic reacquisition is disabled; registration correction is blocked."
            )
        if not configured.admission_enabled:
            raise AdmissionSafetyError(
                "Direct admission is disabled; registration correction is blocked."
            )

        admission = ebook_admission_by_id(admission_id)
        if not admission:
            raise AdmissionSafetyError("Admission record not found.")
        admission_status = str(admission.get("status") or "")
        if admission_status not in _REGISTRATION_CORRECTION_STATUSES:
            raise AdmissionSafetyError(
                f"Admission status '{admission_status}' is not eligible for "
                "registration correction."
            )
        if not admission.get("staged_sha256"):
            raise AdmissionSafetyError(
                "The admission has no verified snapshot to protect."
            )

        destination = _verified_published_destination(admission, configured)
        staged_path = _verified_staged_source(admission)
        stored_path = str(admission["stored_path"])
        expected_book_id = int(admission["book_id"])

        result = result_by_id(int(admission["result_id"]))
        if (
            not result
            or int(result.get("book_id") or 0) != expected_book_id
            or str(result.get("stored_path") or "") != stored_path
            or str(result.get("local_path") or "")
            != str(admission.get("local_path") or "")
        ):
            raise AdmissionSafetyError(
                "The admission no longer matches its original scan result."
            )

        acquisition = ebook_acquisition_by_admission_id(admission_id)
        if (
            not acquisition
            or str(acquisition.get("status") or "") != "admitted"
            or int(acquisition.get("book_id") or 0) != expected_book_id
            or int(acquisition.get("result_id") or 0)
            != int(admission["result_id"])
            or int(acquisition.get("admission_id") or 0) != admission_id
        ):
            raise AdmissionSafetyError(
                "The admission is not linked to one active admitted acquisition."
            )
        queue_id = acquisition.get("queue_id")
        if queue_id is None:
            raise AdmissionSafetyError(
                "The linked acquisition has no exact Bindery queue ID."
            )

        client = client or BinderyClient()
        auto_grab = _bindery_setting(client, "autoGrab.enabled").casefold()
        if auto_grab != "false":
            raise AdmissionSafetyError(
                "Bindery auto-grab must be disabled before registration correction."
            )

        import_mode = _bindery_setting(client, "import.mode").casefold() or "auto"
        if admission_status == "registration_correcting" and import_mode == "auto":
            try:
                client.set_setting("import.mode", "external")
                import_mode = (
                    _bindery_setting(client, "import.mode").casefold() or "auto"
                )
            except BinderyClientError as exc:
                raise AdmissionSafetyError(
                    "An interrupted correction left Bindery outside external import "
                    f"mode, and recovery failed: {exc}"
                ) from exc
        if import_mode != "external":
            raise AdmissionSafetyError(
                "Bindery must be in external import mode before registration correction."
            )

        try:
            target_book = client.get_book(expected_book_id)
            exact_associations = _exact_ebook_associations(stored_path)
        except (BinderyClientError, sqlite3.Error) as exc:
            raise AdmissionSafetyError(
                f"Bindery ownership could not be verified: {exc}"
            ) from exc
        if not _result_matches_book(result, target_book):
            raise AdmissionSafetyError(
                "The intended Bindery book identity changed since the scan."
            )

        target_registered = _book_has_exact_ebook(target_book, stored_path)
        only_target_association = (
            len(exact_associations) == 1
            and int(exact_associations[0].get("book_id") or 0)
            == expected_book_id
        )
        if target_registered and only_target_association:
            update_ebook_admission(admission_id, "registered")
            return {
                "ok": True,
                "admissionId": admission_id,
                "bookId": expected_book_id,
                "status": "registered",
                "alreadyCorrected": True,
                "queueRecordRemoved": False,
                "removedFromDownloadClient": False,
                "downloadedDataDeleted": False,
                "libraryBytesChanged": False,
                "stagedFileRetained": True,
                "message": (
                    "The exact Bindery association was already corrected and is now "
                    "recorded as registered."
                ),
            }
        if target_registered or only_target_association:
            raise AdmissionSafetyError(
                "Bindery's API and database disagree about the exact path owner."
            )
        if _book_has_ebook(target_book):
            raise AdmissionSafetyError(
                "The intended Bindery book already tracks a different ebook."
            )
        if len(exact_associations) != 1:
            raise AdmissionSafetyError(
                "Exactly one conflicting ebook association is required."
            )
        wrong_owner = exact_associations[0]
        if int(wrong_owner.get("book_id") or 0) == expected_book_id:
            raise AdmissionSafetyError(
                "The exact path is not owned by a different Bindery book."
            )

        queue_items = _complete_queue_items(client)
        active_queue = [
            item
            for item in queue_items
            if str(item.get("status") or "").casefold()
            not in _HISTORICAL_QUEUE_STATUSES
        ]
        queue_matches = [
            item
            for item in queue_items
            if str(item.get("id") or "") == str(queue_id)
        ]
        if len(queue_matches) > 1:
            raise AdmissionSafetyError(
                "Multiple Bindery queue records share the acquisition queue ID."
            )
        queue_item = queue_matches[0] if queue_matches else None
        unrelated_active = [
            item
            for item in active_queue
            if str(item.get("id") or "") != str(queue_id)
        ]
        if unrelated_active:
            raise AdmissionSafetyError(
                "Another active Bindery queue record blocks registration correction."
            )
        if queue_item is None and admission_status != "registration_correcting":
            raise AdmissionSafetyError(
                "The exact acquisition queue record is absent; correction stopped."
            )
        if queue_item is not None and (
            int(queue_item.get("bookId") or 0) != expected_book_id
            or str(queue_item.get("status") or "").casefold() != "importexternal"
        ):
            raise AdmissionSafetyError(
                "The exact acquisition queue record is not a matching external import."
            )

        try:
            preview = client.preview_manual_reassignment(
                stored_path,
                expected_book_id,
                file_format="ebook",
            )
        except BinderyClientError as exc:
            raise AdmissionSafetyError(str(exc)) from exc
        if not isinstance(preview, dict):
            raise AdmissionSafetyError(
                "Bindery returned an invalid reassignment preview."
            )
        if (
            os.path.normpath(str(preview.get("source") or ""))
            != os.path.normpath(stored_path)
            or os.path.normpath(str(preview.get("destination") or ""))
            != os.path.normpath(stored_path)
            or str(preview.get("format") or "").casefold() != "ebook"
            or str(preview.get("status") or "").casefold() != "noop"
        ):
            raise AdmissionSafetyError(
                "Bindery's reassignment preview was not an exact no-move ebook operation."
            )

        update_ebook_admission(
            admission_id,
            "registration_correcting",
            error=(
                "An explicit Bindery registration correction is in progress. "
                "Library and staged bytes must remain unchanged."
            ),
        )
        correction_started = True
        if queue_item is not None:
            try:
                client.remove_queue_item(
                    int(queue_id),
                    remove_from_client=False,
                    delete_files=False,
                )
            except BinderyClientError as exc:
                raise AdmissionSafetyError(str(exc)) from exc

        response: dict[str, Any] | None = None
        operation_error: str | None = None
        restore_error: str | None = None
        try:
            client.set_setting("import.mode", "auto")
            response = client.reassign_manual_import(
                stored_path,
                expected_book_id,
                file_format="ebook",
            )
            for _ in range(_REGISTRATION_CORRECTION_POLL_ATTEMPTS):
                exact_associations = _exact_ebook_associations(stored_path)
                target_book = client.get_book(expected_book_id)
                if (
                    len(exact_associations) == 1
                    and int(exact_associations[0].get("book_id") or 0)
                    == expected_book_id
                    and _book_has_exact_ebook(target_book, stored_path)
                ):
                    break
                time.sleep(_REGISTRATION_CORRECTION_POLL_SECONDS)
            else:
                operation_error = (
                    "Bindery did not confirm the corrected exact-path owner in time."
                )
        except (BinderyClientError, sqlite3.Error) as exc:
            operation_error = str(exc)
        finally:
            try:
                client.set_setting("import.mode", "external")
                if _bindery_setting(client, "import.mode").casefold() != "external":
                    restore_error = (
                        "Bindery did not confirm restoration of external import mode."
                    )
            except BinderyClientError as exc:
                restore_error = str(exc)

        if operation_error or restore_error:
            parts = [part for part in (operation_error, restore_error) if part]
            error = (
                "Registration correction did not complete safely: "
                + " ".join(parts)
                + " The durable correction state and both byte copies were retained."
            )
            update_ebook_admission(
                admission_id,
                "registration_correcting",
                error=error,
            )
            raise AdmissionSafetyError(error)

        # Repeat every byte and ownership check after Bindery reports success.
        _verified_published_destination(admission, configured)
        _verified_staged_source(admission)
        try:
            final_book = client.get_book(expected_book_id)
            final_associations = _exact_ebook_associations(stored_path)
        except (BinderyClientError, sqlite3.Error) as exc:
            error = f"Corrected ownership could not be independently confirmed: {exc}"
            update_ebook_admission(
                admission_id,
                "registration_correcting",
                error=error,
            )
            raise AdmissionSafetyError(error) from exc
        if (
            len(final_associations) != 1
            or int(final_associations[0].get("book_id") or 0)
            != expected_book_id
            or not _book_has_exact_ebook(final_book, stored_path)
            or _bindery_setting(client, "import.mode").casefold() != "external"
            or _bindery_setting(client, "autoGrab.enabled").casefold() != "false"
            or sha256_file(destination) != admission["staged_sha256"]
            or sha256_file(staged_path) != admission["staged_sha256"]
        ):
            error = (
                "Post-correction safety verification failed; the durable correction "
                "state was retained for explicit recovery."
            )
            update_ebook_admission(
                admission_id,
                "registration_correcting",
                error=error,
            )
            raise AdmissionSafetyError(error)

        update_ebook_admission(admission_id, "registered")
        return {
            "ok": True,
            "admissionId": admission_id,
            "bookId": expected_book_id,
            "status": "registered",
            "alreadyCorrected": False,
            "formerOwner": wrong_owner,
            "reassignResponse": response,
            "queueRecordRemoved": queue_item is not None,
            "removedFromDownloadClient": False,
            "downloadedDataDeleted": False,
            "libraryBytesChanged": False,
            "stagedFileRetained": True,
            "message": (
                "Bindery now assigns the exact admitted path to the intended book. "
                "The coordinator may continue with safe finalization."
            ),
        }
    except AdmissionSafetyError as exc:
        if correction_started:
            update_ebook_admission(
                admission_id,
                "registration_correcting",
                error=(
                    f"{exc} The explicit correction may be safely retried after "
                    "reviewing this error."
                ),
            )
        raise
    except (BinderyClientError, sqlite3.Error, OSError) as exc:
        error = str(exc)
        if correction_started:
            update_ebook_admission(
                admission_id,
                "registration_correcting",
                error=(
                    "Registration correction was interrupted: "
                    f"{error}. Explicit recovery is required."
                ),
            )
        raise AdmissionSafetyError(error) from exc
    finally:
        _admission_lock.release()


def admission_history(limit: int = 100) -> dict[str, Any]:
    items = recent_ebook_admissions(limit)
    return {"items": items, "count": len(items)}

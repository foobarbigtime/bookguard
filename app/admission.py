from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import threading
from typing import Any

from .bindery_client import BinderyClient, BinderyClientError
from .config import ConfigurationError, load_automation_settings, settings
from .db import (
    create_ebook_admission,
    ebook_admission_by_id,
    recent_ebook_admissions,
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


_admission_lock = threading.Lock()
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

        os.fsync(destination_directory_fd)
        if not renamed:
            os.unlink(temp_path.name, dir_fd=source_directory_fd)
            os.fsync(source_directory_fd)
    finally:
        os.close(source_directory_fd)
        os.close(destination_directory_fd)
    _cleanup_private_snapshot(temp_path)
    return publication_method


def _cleanup_private_snapshot(temp_path: Path | None) -> None:
    if temp_path is None:
        return
    temp_path.unlink(missing_ok=True)
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
        update_ebook_admission(admission_id, "published")

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


def reconcile_admission(
    admission_id: int,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Confirm Bindery registered the published bytes; never delete staging."""
    admission = ebook_admission_by_id(admission_id)
    if not admission:
        raise AdmissionSafetyError("Admission record not found.")
    if not admission.get("staged_sha256"):
        raise AdmissionSafetyError("The admission has no verified snapshot to reconcile.")

    configured = _automation_settings()
    try:
        relative = Path(admission["stored_path"]).relative_to(
            Path(configured.admission_bindery_root)
        )
    except ValueError as exc:
        raise AdmissionSafetyError(
            "The recorded admission path is outside the configured Bindery root."
        ) from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise AdmissionSafetyError("The recorded admission path contains an unsafe component.")
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
        raise AdmissionSafetyError("The recorded destination resolves outside admission.") from exc
    if not destination.is_file() or destination.is_symlink():
        raise AdmissionSafetyError("The published library file is missing or symlinked.")
    try:
        destination_hash = sha256_file(destination)
    except OSError as exc:
        raise AdmissionSafetyError("The published library file could not be read.") from exc
    if destination_hash != admission["staged_sha256"]:
        raise AdmissionSafetyError(
            "The published library file no longer matches the verified snapshot."
        )

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
        "stagingRetained": True,
        "message": (
            "Bindery registration is confirmed; the staged source remains available for manual cleanup."
            if registered
            else "Bindery has not registered the file yet; a library scan was requested."
        ),
    }


def admission_history(limit: int = 100) -> dict[str, Any]:
    items = recent_ebook_admissions(limit)
    return {"items": items, "count": len(items)}

from __future__ import annotations

from pathlib import Path
from typing import Any

from .bindery_client import BinderyClient
from .config import ConfigurationError, load_automation_settings, settings
from .ebook_extraction import extract_ebook_identity
from .ebook_security import inspect_ebook_security
from .file_safety import sha256_file
from .verification_engine import classify_identity


class StagingSafetyError(RuntimeError):
    pass


SUPPORTED_STAGED_EBOOK_SUFFIXES = {".epub", ".pdf", ".rtf", ".txt"}
MIN_ADMISSION_CONFIDENCE = 99


def _staging_root() -> Path:
    try:
        configured = load_automation_settings()
    except ConfigurationError as exc:
        raise StagingSafetyError(str(exc)) from exc
    root = Path(configured.staging_root).resolve()
    if not root.is_dir():
        raise StagingSafetyError("The configured staging root does not exist or is not a directory.")
    return root


def _max_staged_ebook_bytes() -> int:
    try:
        return load_automation_settings().max_staged_ebook_bytes
    except ConfigurationError as exc:
        raise StagingSafetyError(str(exc)) from exc


def resolve_staged_file(relative_path: str) -> tuple[Path, Path]:
    root = _staging_root()
    supplied = str(relative_path or "").strip()
    if not supplied:
        raise StagingSafetyError("A staging-relative file path is required.")

    requested = Path(supplied)
    if requested.is_absolute():
        raise StagingSafetyError("The staged file path must be relative to the staging root.")

    lexical = root / requested
    try:
        if lexical.is_symlink():
            raise StagingSafetyError("Symlinked staged files are not accepted.")
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except StagingSafetyError:
        raise
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise StagingSafetyError(
            "The staged file does not exist or resolves outside the staging root."
        ) from exc

    if not resolved.is_file():
        raise StagingSafetyError("The staged path must identify one regular ebook file.")
    if resolved.suffix.lower() not in SUPPORTED_STAGED_EBOOK_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_STAGED_EBOOK_SUFFIXES))
        raise StagingSafetyError(f"Unsupported staged ebook format; supported formats: {supported}.")
    if resolved.stat().st_size <= 0:
        raise StagingSafetyError("The staged ebook is empty.")
    if resolved.stat().st_size > _max_staged_ebook_bytes():
        raise StagingSafetyError("The staged ebook exceeds the configured verification size limit.")
    return root, resolved


def staged_path_is_absent(relative_path: str) -> bool:
    """Return true only when a safe staging-relative path is genuinely absent."""
    root = _staging_root()
    supplied = str(relative_path or "").strip()
    if not supplied:
        raise StagingSafetyError("A staging-relative file path is required.")

    requested = Path(supplied)
    if requested.is_absolute() or any(part in {"", ".", ".."} for part in requested.parts):
        raise StagingSafetyError("The staged file path must be a safe relative path.")

    lexical = root / requested
    if lexical.is_symlink():
        raise StagingSafetyError("Symlinked staged files are not accepted.")
    if lexical.exists():
        return False

    try:
        lexical.parent.resolve(strict=True).relative_to(root)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise StagingSafetyError(
            "The staged file parent does not exist or resolves outside the staging root."
        ) from exc
    return True


def book_identity(book: dict[str, Any]) -> tuple[str, str]:
    author_obj = book.get("author") if isinstance(book.get("author"), dict) else {}
    title = str(book.get("title") or "").strip()
    author = str(
        book.get("authorName")
        or author_obj.get("name")
        or author_obj.get("authorName")
        or ""
    ).strip()
    if not title or not author:
        raise StagingSafetyError("Bindery did not provide both an expected title and author.")
    return title, author


def verify_ebook_file(
    book_id: int,
    path: Path,
    *,
    display_path: str,
    book: dict[str, Any],
) -> dict[str, Any]:
    """Verify one already-resolved regular ebook file against a Bindery book."""
    expected_title, expected_author = book_identity(book)
    before_stat = path.stat()
    before_hash = sha256_file(path)
    identity = {
        "book_id": int(book_id),
        "title": expected_title,
        "author": expected_author,
    }
    security = inspect_ebook_security(
        path,
        check_file_signatures=settings.verification_file_signatures,
        check_archive_safety=settings.verification_archive_safety,
        check_epub_structure=settings.verification_epub_structure,
        check_pdf_integrity=settings.verification_pdf_integrity,
    )
    if security["safe"]:
        extracted = extract_ebook_identity(str(path))
        verdict, confidence, evidence = classify_identity(
            identity,
            extracted.metadata,
            extracted.text,
            extracted.identifiers,
            extracted.notes,
            extracted.front_text,
        )
        evidence["security"] = security
        source = extracted.source
    else:
        verdict = "UNSAFE_FILE"
        confidence = 100
        source = "deterministic-safety"
        evidence = {
            "expected": {"title": expected_title, "author": expected_author},
            "embedded": {},
            "content": {},
            "metadata_matches_expected": False,
            "security": security,
            "notes": [security["message"]],
            "explanation": (
                "Book identity was not evaluated because deterministic file safety "
                "or integrity validation failed."
            ),
        }

    after_stat = path.stat()
    after_hash = sha256_file(path)
    stable = (
        before_hash == after_hash
        and before_stat.st_size == after_stat.st_size
        and before_stat.st_mtime_ns == after_stat.st_mtime_ns
        and before_stat.st_ctime_ns == after_stat.st_ctime_ns
        and before_stat.st_dev == after_stat.st_dev
        and before_stat.st_ino == after_stat.st_ino
    )
    safe_to_admit = (
        stable
        and verdict == "VERIFIED_CORRECT"
        and int(confidence) >= MIN_ADMISSION_CONFIDENCE
    )

    blockers: list[str] = []
    if not stable:
        blockers.append("stagedFileChangedDuringVerification")
    if verdict != "VERIFIED_CORRECT":
        blockers.append(f"verdict:{verdict}")
    if not security["safe"]:
        blockers.append("deterministicSecurityChecksFailed")
    if int(confidence) < MIN_ADMISSION_CONFIDENCE:
        blockers.append(f"confidenceBelow:{MIN_ADMISSION_CONFIDENCE}")

    return {
        "bookId": int(book_id),
        "relativePath": display_path,
        "size": after_stat.st_size,
        "sha256": after_hash,
        "stableDuringVerification": stable,
        "expectedTitle": expected_title,
        "expectedAuthor": expected_author,
        "verdict": verdict,
        "confidence": int(confidence),
        "source": source,
        "evidence": evidence,
        "safeToAdmit": safe_to_admit,
        "admissionBlockers": blockers,
    }


def list_staged_ebooks(limit: int = 500) -> dict[str, Any]:
    """List verification-supported files without reading their contents."""
    root = _staging_root()
    capped_limit = max(1, min(int(limit), 1000))
    items: list[dict[str, Any]] = []
    truncated = False

    for candidate in sorted(root.rglob("*"), key=lambda item: str(item).casefold()):
        if candidate.name.startswith("."):
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            stat = resolved.stat()
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            continue
        if resolved.suffix.lower() not in SUPPORTED_STAGED_EBOOK_SUFFIXES:
            continue
        if len(items) >= capped_limit:
            truncated = True
            break
        items.append({
            "relativePath": resolved.relative_to(root).as_posix(),
            "size": stat.st_size,
            "modifiedNs": stat.st_mtime_ns,
            "suffix": resolved.suffix.lower(),
        })

    return {
        "stagingRoot": str(root),
        "items": items,
        "count": len(items),
        "truncated": truncated,
        "readOnly": True,
    }


def verify_staged_ebook(
    book_id: int,
    relative_path: str,
    client: BinderyClient | None = None,
) -> dict[str, Any]:
    """Verify staged ebook bytes against one explicit Bindery book.

    This function never imports, moves, deletes, or modifies the staged file.
    An admission operation must repeat verification at its own mutation
    boundary; this read-only result is not a durable authorization token.
    """
    root, path = resolve_staged_file(relative_path)
    client = client or BinderyClient()
    book = client.get_book(int(book_id))
    result = verify_ebook_file(
        book_id,
        path,
        display_path=path.relative_to(root).as_posix(),
        book=book,
    )
    return {
        **result,
        "readOnly": True,
        "message": (
            "Staged bytes independently verify the intended book. Admission still requires "
            "separate readiness checks and explicit confirmation."
            if result["safeToAdmit"]
            else "Staged bytes did not meet the automatic admission safety threshold."
        ),
    }

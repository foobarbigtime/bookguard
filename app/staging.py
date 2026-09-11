from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from . import verifier as legacy_verifier
from . import verifier_v048 as verifier
from . import verifier_v048_final as _final_verifier  # noqa: F401 - installs final classifier
from .bindery_client import BinderyClient
from .config import settings
from .metadata import ebook_metadata


class StagingSafetyError(RuntimeError):
    pass


SUPPORTED_STAGED_EBOOK_SUFFIXES = {".epub", ".pdf", ".rtf", ".txt"}
MIN_ADMISSION_CONFIDENCE = 99
DEFAULT_MAX_STAGED_EBOOK_BYTES = 512 * 1024 * 1024


def _staging_root() -> Path:
    root = Path(os.getenv("BOOKGUARD_STAGING_ROOT", "/staging")).resolve()
    if not root.is_dir():
        raise StagingSafetyError("The configured staging root does not exist or is not a directory.")
    return root


def _max_staged_ebook_bytes() -> int:
    raw = os.getenv("BOOKGUARD_MAX_STAGED_EBOOK_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_STAGED_EBOOK_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise StagingSafetyError("BOOKGUARD_MAX_STAGED_EBOOK_BYTES must be an integer.") from exc
    if value <= 0:
        raise StagingSafetyError("BOOKGUARD_MAX_STAGED_EBOOK_BYTES must be greater than zero.")
    return value


def _resolve_staged_file(relative_path: str) -> tuple[Path, Path]:
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _book_identity(book: dict[str, Any]) -> tuple[str, str]:
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
    A future admission operation must repeat verification at its own mutation
    boundary; this read-only result is not a durable authorization token.
    """
    root, path = _resolve_staged_file(relative_path)
    client = client or BinderyClient()
    book = client.get_book(int(book_id))
    expected_title, expected_author = _book_identity(book)

    before_stat = path.stat()
    before_hash = _sha256(path)
    suffix = path.suffix.lower()
    metadata = ebook_metadata(str(path))
    text = ""
    front_text = ""
    identifiers: list[str] = []
    source = "native"
    notes: list[str] = []

    try:
        if suffix == ".epub":
            metadata, text, identifiers, front_text = verifier._epub_identity(str(path))
            source = "native-epub"
        elif suffix == ".pdf":
            metadata, text, identifiers, front_text = verifier._pdf_identity(str(path))
            source = "native-pdf"
        elif suffix in {".txt", ".rtf"}:
            metadata, text, identifiers, front_text = verifier._plain_identity(str(path))
            source = f"native-{suffix.lstrip('.')}"
    except Exception as exc:
        notes.append(f"Native extraction error: {str(exc)[:300]}")

    if (
        len(text.strip()) < verifier.MIN_USEFUL_TEXT
        and settings.verification_use_tika
        and settings.verification_tika_url
    ):
        tika_text, tika_error = legacy_verifier._tika_text(str(path))
        if tika_text:
            text = tika_text
            front_text = tika_text[: verifier.FRONT_TEXT_CHARS]
            source = f"{source}+tika" if source != "native" else "tika"
            notes.append("Tika fallback supplied content because native extraction was insufficient.")
        elif tika_error:
            notes.append(tika_error)

    identity = {
        "book_id": int(book_id),
        "title": expected_title,
        "author": expected_author,
    }
    verdict, confidence, evidence = verifier._classify_identity(
        identity,
        metadata,
        text,
        identifiers,
        source,
        notes,
        front_text,
    )

    after_stat = path.stat()
    after_hash = _sha256(path)
    stable = (
        before_hash == after_hash
        and before_stat.st_size == after_stat.st_size
        and before_stat.st_mtime_ns == after_stat.st_mtime_ns
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
    if int(confidence) < MIN_ADMISSION_CONFIDENCE:
        blockers.append(f"confidenceBelow:{MIN_ADMISSION_CONFIDENCE}")

    return {
        "bookId": int(book_id),
        "relativePath": path.relative_to(root).as_posix(),
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
        "readOnly": True,
        "message": (
            "Staged bytes independently verify the intended book. Admission remains disabled in this slice."
            if safe_to_admit
            else "Staged bytes did not meet the automatic admission safety threshold."
        ),
    }

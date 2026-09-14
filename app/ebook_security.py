from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote
import zipfile
import xml.etree.ElementTree as ET

from pypdf import PdfReader


ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
RAR_SIGNATURES = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")


def _result(status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"status": status, "message": message, **details}


def _detected_format(head: bytes) -> str:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(ZIP_SIGNATURES):
        return "zip"
    if head.startswith(RAR_SIGNATURES):
        return "rar"
    if len(head) >= 68 and head[60:68] == b"BOOKMOBI":
        return "mobi"
    if head.lstrip(b"\xef\xbb\xbf\t\r\n ").startswith(b"{\\rtf"):
        return "rtf"
    return "unknown"


def _signature_check(path: Path, suffix: str, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return _result("disabled", "File-signature validation is disabled.")

    expected = {
        ".epub": "zip",
        ".pdf": "pdf",
        ".mobi": "mobi",
        ".azw": "mobi",
        ".azw3": "mobi",
        ".cbz": "zip",
        ".cbr": "rar",
        ".rtf": "rtf",
    }.get(suffix)
    if expected is None:
        return _result(
            "not_applicable",
            f"{suffix or 'This file type'} has no fixed signature BookGuard can validate.",
            expectedFormat=suffix.removeprefix(".") or "unknown",
            detectedFormat=None,
        )

    with path.open("rb") as handle:
        head = handle.read(80)
    detected = _detected_format(head)
    if detected != expected:
        return _result(
            "failed",
            f"The bytes identify as {detected}, not the expected {expected} format.",
            expectedFormat=expected,
            detectedFormat=detected,
        )
    return _result(
        "passed",
        f"The file signature matches the expected {expected} format.",
        expectedFormat=expected,
        detectedFormat=detected,
    )


def _safe_archive_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _epub_structure_check(path: Path, suffix: str, enabled: bool) -> dict[str, Any]:
    if suffix != ".epub":
        return _result("not_applicable", "EPUB structure validation does not apply.")
    if not enabled:
        return _result("disabled", "EPUB structure validation is disabled.")

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            name_set = set(names)
            if len(names) != len(name_set):
                raise ValueError("the archive contains duplicate member names")
            if archive.testzip() is not None:
                raise ValueError("a ZIP member failed its CRC integrity check")
            if "mimetype" not in name_set:
                raise ValueError("the required mimetype file is absent")
            mimetype_info = archive.getinfo("mimetype")
            if names[0] != "mimetype" or mimetype_info.compress_type != zipfile.ZIP_STORED:
                raise ValueError("the mimetype file is not first and uncompressed")
            if archive.read("mimetype") != b"application/epub+zip":
                raise ValueError("the mimetype file is not application/epub+zip")
            if "META-INF/container.xml" not in name_set:
                raise ValueError("META-INF/container.xml is absent")

            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfiles = [
                element.attrib.get("full-path", "").strip()
                for element in container.iter()
                if _local_name(element.tag) == "rootfile"
            ]
            package_path = next((item for item in rootfiles if item), "")
            if not _safe_archive_path(package_path):
                raise ValueError("the package path is absent or unsafe")
            if package_path not in name_set:
                raise ValueError("the package document referenced by container.xml is absent")

            package = ET.fromstring(archive.read(package_path))
            if _local_name(package.tag) != "package":
                raise ValueError("the referenced package document has the wrong root element")

            manifest = {
                element.attrib.get("id", "").strip(): element.attrib.get("href", "").strip()
                for element in package.iter()
                if _local_name(element.tag) == "item"
                and element.attrib.get("id", "").strip()
                and element.attrib.get("href", "").strip()
            }
            spine_ids = [
                element.attrib.get("idref", "").strip()
                for element in package.iter()
                if _local_name(element.tag) == "itemref"
            ]
            if not manifest:
                raise ValueError("the package manifest is empty")
            if not spine_ids:
                raise ValueError("the package spine is empty")
            missing_spine_ids = sorted({item for item in spine_ids if item not in manifest})
            if missing_spine_ids:
                raise ValueError("the package spine references missing manifest items")
            package_parent = PurePosixPath(package_path).parent
            for spine_id in spine_ids:
                href = unquote(manifest[spine_id].split("#", 1)[0])
                resource = package_parent / PurePosixPath(href)
                resource_name = resource.as_posix()
                if not _safe_archive_path(resource_name) or resource_name not in name_set:
                    raise ValueError("the package spine references an absent or unsafe resource")
    except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError, RuntimeError) as exc:
        return _result("failed", f"EPUB structure is invalid: {exc}")

    return _result(
        "passed",
        "The EPUB ZIP, mimetype, container, package, manifest, spine, and CRC checks passed.",
        packagePath=package_path,
        manifestItems=len(manifest),
        spineItems=len(spine_ids),
    )


def _pdf_integrity_check(path: Path, suffix: str, enabled: bool) -> dict[str, Any]:
    if suffix != ".pdf":
        return _result("not_applicable", "PDF integrity validation does not apply.")
    if not enabled:
        return _result("disabled", "PDF integrity validation is disabled.")

    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError("the PDF header is absent")
            handle.seek(max(0, path.stat().st_size - 4096))
            trailer = handle.read()
        if b"%%EOF" not in trailer:
            raise ValueError("the PDF end-of-file marker is absent")
        reader = PdfReader(str(path), strict=False)
        pages = len(reader.pages)
        if pages < 1:
            raise ValueError("the PDF has no pages")
        encrypted = bool(reader.is_encrypted)
    except (OSError, ValueError, RuntimeError) as exc:
        return _result("failed", f"PDF integrity is invalid: {exc}")
    except Exception as exc:
        return _result("failed", f"PDF parsing failed: {exc}")

    return _result(
        "passed",
        "The PDF header, cross-reference structure, page tree, and EOF marker passed.",
        pages=pages,
        encrypted=encrypted,
    )


def inspect_ebook_security(
    path: str | Path,
    *,
    check_file_signatures: bool = True,
    check_epub_structure: bool = True,
    check_pdf_integrity: bool = True,
) -> dict[str, Any]:
    """Run deterministic, read-only ebook type and integrity checks."""
    target = Path(path)
    suffix = target.suffix.lower()
    checks = {
        "fileSignature": _signature_check(target, suffix, check_file_signatures),
        "epubStructure": _epub_structure_check(target, suffix, check_epub_structure),
        "pdfIntegrity": _pdf_integrity_check(target, suffix, check_pdf_integrity),
    }
    failures = [name for name, check in checks.items() if check["status"] == "failed"]
    safe = not failures
    return {
        "safe": safe,
        "readOnly": True,
        "expectedFormat": suffix.removeprefix(".") or "unknown",
        "checks": checks,
        "failures": failures,
        "message": (
            "All applicable deterministic ebook safety checks passed."
            if safe
            else "One or more deterministic ebook safety checks failed."
        ),
    }

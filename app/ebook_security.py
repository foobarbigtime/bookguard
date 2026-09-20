from __future__ import annotations

from pathlib import Path, PurePosixPath
import stat
from typing import Any
from urllib.parse import unquote
import zipfile
import xml.etree.ElementTree as ET

from . import archive_probe, malware_scan, pdf_probe
from .archive_io import read_zip_member_bounded


ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
RAR_SIGNATURES = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")
ARCHIVE_SUFFIXES = {".epub", ".cbz"}
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBER_RATIO = 1000
MAX_ARCHIVE_TOTAL_RATIO = 500
MIN_RATIO_CHECK_BYTES = 16 * 1024 * 1024
MIN_TOTAL_RATIO_CHECK_BYTES = 64 * 1024 * 1024
EPUB_MIMETYPE_MAX_BYTES = 256


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


def _archive_safety_check(path: Path, suffix: str, enabled: bool) -> dict[str, Any]:
    if suffix not in ARCHIVE_SUFFIXES:
        return _result("not_applicable", "Archive safety validation does not apply.")
    if not enabled:
        return _result("disabled", "Archive safety validation is disabled.")

    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(
                    f"the archive has {len(members)} members; the limit is "
                    f"{MAX_ARCHIVE_MEMBERS}"
                )

            exact_names: set[str] = set()
            portable_names: dict[str, str] = {}
            total_uncompressed = 0
            total_compressed = 0
            highest_ratio = 0.0

            for member in members:
                raw_name = member.filename
                normalized = raw_name.replace("\\", "/")
                parts = PurePosixPath(normalized).parts
                if (
                    not raw_name
                    or "\\" in raw_name
                    or normalized.startswith("/")
                    or any(part in {".", ".."} for part in parts)
                    or (parts and parts[0].endswith(":"))
                ):
                    raise ValueError(f"unsafe member path: {raw_name!r}")

                if raw_name in exact_names:
                    raise ValueError(f"duplicate member name: {raw_name!r}")
                exact_names.add(raw_name)

                portable_name = normalized.rstrip("/").casefold()
                previous = portable_names.get(portable_name)
                if previous is not None and previous != raw_name:
                    raise ValueError(
                        f"case-colliding member names: {previous!r} and {raw_name!r}"
                    )
                portable_names[portable_name] = raw_name

                unix_mode = (member.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(unix_mode):
                    raise ValueError(f"symbolic-link member: {raw_name!r}")
                if member.flag_bits & 0x1:
                    raise ValueError(f"encrypted member: {raw_name!r}")
                if member.is_dir():
                    continue
                if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError(
                        f"member {raw_name!r} expands beyond the 1 GiB per-file limit"
                    )

                total_uncompressed += member.file_size
                total_compressed += member.compress_size
                if total_uncompressed > MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError("the archive expands beyond the 2 GiB total limit")

                ratio = member.file_size / max(1, member.compress_size)
                highest_ratio = max(highest_ratio, ratio)
                if (
                    member.file_size >= MIN_RATIO_CHECK_BYTES
                    and ratio > MAX_ARCHIVE_MEMBER_RATIO
                ):
                    raise ValueError(
                        f"member {raw_name!r} has a dangerous {ratio:.0f}:1 expansion ratio"
                    )

            total_ratio = total_uncompressed / max(1, total_compressed)
            if (
                total_uncompressed >= MIN_TOTAL_RATIO_CHECK_BYTES
                and total_ratio > MAX_ARCHIVE_TOTAL_RATIO
            ):
                raise ValueError(
                    f"the archive has a dangerous {total_ratio:.0f}:1 total expansion ratio"
                )
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        return _result("failed", f"Archive safety validation failed: {exc}")

    return _result(
        "passed",
        "Archive paths, links, encryption, member counts, sizes, and expansion ratios passed.",
        members=len(members),
        uncompressedBytes=total_uncompressed,
        compressedBytes=total_compressed,
        totalExpansionRatio=round(total_ratio, 2),
        highestMemberExpansionRatio=round(highest_ratio, 2),
        limits={
            "members": MAX_ARCHIVE_MEMBERS,
            "memberBytes": MAX_ARCHIVE_MEMBER_BYTES,
            "totalBytes": MAX_ARCHIVE_TOTAL_BYTES,
            "memberExpansionRatio": MAX_ARCHIVE_MEMBER_RATIO,
            "totalExpansionRatio": MAX_ARCHIVE_TOTAL_RATIO,
        },
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _epub_structure_check(path: Path, suffix: str, enabled: bool) -> dict[str, Any]:
    if suffix != ".epub":
        return _result("not_applicable", "EPUB structure validation does not apply.")
    if not enabled:
        return _result("disabled", "EPUB structure validation is disabled.")

    try:
        with zipfile.ZipFile(path) as archive:
            warnings: list[str] = []
            names = archive.namelist()
            name_set = set(names)
            if len(names) != len(name_set):
                raise ValueError("the archive contains duplicate member names")
            crc_result = archive_probe.inspect_zip_crc(path)
            if crc_result.get("error"):
                raise ValueError(
                    "the ZIP CRC integrity check could not complete: "
                    f"{str(crc_result['error'])[:500]}"
                )
            if crc_result.get("badMember") is not None:
                raise ValueError("a ZIP member failed its CRC integrity check")
            if "mimetype" not in name_set:
                raise ValueError("the required mimetype file is absent")
            mimetype_info = archive.getinfo("mimetype")
            expected_mimetype = b"application/epub+zip"
            mimetype_value = read_zip_member_bounded(
                archive,
                "mimetype",
                max_bytes=EPUB_MIMETYPE_MAX_BYTES,
            )
            if mimetype_value != expected_mimetype:
                normalized_mimetype = mimetype_value.removeprefix(b"\xef\xbb\xbf").strip(
                    b" \t\r\n\v\f"
                )
                if normalized_mimetype != expected_mimetype:
                    raise ValueError("the mimetype file is not application/epub+zip")
                warnings.append(
                    "The mimetype file contains a UTF-8 BOM or surrounding ASCII whitespace."
                )
            if names[0] != "mimetype":
                warnings.append("The mimetype file is not the first archive member.")
            if mimetype_info.compress_type != zipfile.ZIP_STORED:
                warnings.append("The mimetype file is compressed instead of stored.")
            if "META-INF/container.xml" not in name_set:
                raise ValueError("META-INF/container.xml is absent")

            container = ET.fromstring(
                read_zip_member_bounded(archive, "META-INF/container.xml")
            )
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

            package = ET.fromstring(read_zip_member_bounded(archive, package_path))
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
        "warning" if warnings else "passed",
        (
            "The EPUB structure and integrity checks passed with a non-blocking "
            "packaging conformance warning."
            if warnings
            else "The EPUB ZIP, mimetype, container, package, manifest, spine, and CRC "
            "checks passed."
        ),
        packagePath=package_path,
        manifestItems=len(manifest),
        spineItems=len(spine_ids),
        warnings=warnings,
    )


def _pdf_integrity_check(path: Path, suffix: str, enabled: bool) -> dict[str, Any]:
    if suffix != ".pdf":
        return _result("not_applicable", "PDF integrity validation does not apply.")
    if not enabled:
        return _result("disabled", "PDF integrity validation is disabled.")

    payload = pdf_probe.inspect_pdf_integrity(path)
    if payload.get("error"):
        return _result(
            "failed",
            f"PDF integrity is invalid: {str(payload['error'])[:500]}",
        )

    pages = int(payload.get("pages") or 0)
    if pages < 1:
        return _result("failed", "PDF integrity is invalid: the PDF has no pages")
    encrypted = bool(payload.get("encrypted"))

    return _result(
        "passed",
        "The PDF header, cross-reference structure, page tree, and EOF marker passed.",
        pages=pages,
        encrypted=encrypted,
    )



def _malware_scan_check(
    path: Path,
    *,
    enabled: bool,
    blocked: bool,
    host: str,
    port: int,
    timeout_seconds: int,
    max_bytes: int,
) -> dict[str, Any]:
    if not enabled:
        return _result("disabled", "Malware scanning is disabled.")
    if blocked:
        return _result(
            "blocked",
            "Malware scanning was not attempted because an earlier deterministic safety check failed.",
        )

    payload = malware_scan.scan_with_clamd(
        path,
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )
    if payload.get("error"):
        return _result("failed", str(payload["error"])[:500], engine="clamd")
    if payload.get("infected"):
        return _result(
            "failed",
            "ClamAV identified the file as malware.",
            engine="clamd",
            signature=str(payload.get("signature") or "unknown")[:500],
            scannedBytes=int(payload.get("size") or 0),
        )
    return _result(
        "passed",
        "ClamAV reported the file clean.",
        engine="clamd",
        scannedBytes=int(payload.get("size") or 0),
    )

def inspect_ebook_security(
    path: str | Path,
    *,
    check_file_signatures: bool = True,
    check_archive_safety: bool = True,
    check_epub_structure: bool = True,
    check_pdf_integrity: bool = True,
    check_malware: bool = False,
    clamd_host: str = "",
    clamd_port: int = 3310,
    malware_timeout_seconds: int = 60,
    malware_max_bytes: int = 512 * 1024 * 1024,
) -> dict[str, Any]:
    """Run deterministic, read-only ebook type, integrity, and optional malware checks."""
    target = Path(path)
    suffix = target.suffix.lower()
    file_signature = _signature_check(target, suffix, check_file_signatures)
    archive_safety = _archive_safety_check(target, suffix, check_archive_safety)
    epub_structure = (
        _result(
            "blocked",
            "EPUB structure validation was not attempted because archive safety failed.",
        )
        if suffix == ".epub" and archive_safety["status"] == "failed"
        else _epub_structure_check(target, suffix, check_epub_structure)
    )
    pdf_integrity = _pdf_integrity_check(target, suffix, check_pdf_integrity)
    core_checks = {
        "fileSignature": file_signature,
        "archiveSafety": archive_safety,
        "epubStructure": epub_structure,
        "pdfIntegrity": pdf_integrity,
    }
    core_failed = any(check["status"] == "failed" for check in core_checks.values())
    malware = _malware_scan_check(
        target,
        enabled=check_malware,
        blocked=core_failed,
        host=clamd_host,
        port=clamd_port,
        timeout_seconds=malware_timeout_seconds,
        max_bytes=malware_max_bytes,
    )
    checks = {**core_checks, "malwareScan": malware}
    failures = [name for name, check in checks.items() if check["status"] == "failed"]
    safe = not failures
    return {
        "safe": safe,
        "readOnly": True,
        "expectedFormat": suffix.removeprefix(".") or "unknown",
        "checks": checks,
        "failures": failures,
        "message": (
            "All enabled ebook safety checks passed."
            if safe
            else "One or more enabled ebook safety checks failed."
        ),
    }

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import zipfile
import xml.etree.ElementTree as ET

from mutagen import File as MutagenFile

from .config import settings
from .db import (
    create_metadata_repair,
    finish_metadata_repair,
    latest_scan,
    mark_metadata_repair_undone,
    metadata_repair_by_id,
)
from .matcher import (
    author_match_strict,
    author_mentioned_in_text,
    normalize,
)
from .metadata import AUDIO_EXTENSIONS, ebook_metadata


class RepairError(RuntimeError):
    pass


def require_current_scan_result(result: dict) -> None:
    """Refuse a write based on stale or incomplete scan evidence."""
    scan = latest_scan()
    if not scan or scan.get("status") != "complete":
        raise RepairError("A completed latest scan is required before metadata repair.")
    if result.get("scan_id") != scan.get("id"):
        raise RepairError(
            "This result is not from the latest completed scan. Refresh before metadata repair."
        )


WRITABLE_AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".ogg", ".opus"}
EBOOK_CANDIDATE_SUFFIXES = {
    ".epub", ".pdf", ".mobi", ".azw", ".azw3", ".cbz", ".rtf", ".txt", ".cbr", ".lit",
}


def _first(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value)


def _same_text(left: str | None, right: str | None) -> bool:
    return normalize(left) == normalize(right)


def _same_author_person(expected: str | None, observed: str | None) -> bool:
    """Match the same literal name with token order ignored, but do not collapse pen names/aliases."""
    e = normalize(expected)
    o = normalize(observed)
    if not e or not o:
        return False
    if e == o:
        return True
    e_tokens = e.split()
    o_tokens = o.split()
    return len(e_tokens) >= 2 and len(e_tokens) == len(o_tokens) and sorted(e_tokens) == sorted(o_tokens)


def _repair_title_equivalent(expected: str | None, observed: str | None) -> bool:
    """Safe normalization requires an exact normalized title, not fuzzy scan equivalence."""
    e = normalize(expected)
    o = normalize(observed)
    return bool(e) and e == o


def _resolve_ebook_target(local_path: str) -> str:
    path = Path(local_path)
    if path.is_file():
        return str(path)
    if not path.is_dir():
        return local_path
    candidates = sorted(
        (
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file() and candidate.suffix.lower() in EBOOK_CANDIDATE_SUFFIXES
        ),
        key=lambda candidate: str(candidate).casefold(),
    )
    return str(candidates[0]) if candidates else local_path


def _all_audio_paths(local_path: str) -> list[str]:
    path = Path(local_path)
    if path.is_file():
        return [str(path)] if path.suffix.lower() in AUDIO_EXTENSIONS else []
    if not path.is_dir():
        return []
    found: list[str] = []
    for root, _, names in os.walk(path):
        for name in names:
            candidate = Path(root) / name
            if candidate.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(str(candidate))
    return sorted(found, key=str.casefold)


def _audio_summary_safe(result: dict) -> bool:
    metadata = result.get("metadata") or {}
    detected_title = str(metadata.get("detected_title") or "")
    detected_author = str(metadata.get("detected_author") or "")
    if not _repair_title_equivalent(result["title"], detected_title):
        return False
    if author_match_strict(result["author"], detected_author):
        return True
    return author_mentioned_in_text(result["author"], detected_title)


def repair_candidate_summary(result: dict) -> dict:
    """Cheap, scan-result-only determination used by the dashboard."""
    if settings.metadata_repair_mode == "off":
        return {"eligible": False, "safe": False, "kind": "", "reason": "Metadata repair is off."}
    if result.get("classification") in {"REJECT", "MISSING"}:
        return {
            "eligible": False,
            "safe": False,
            "kind": "",
            "reason": "BookGuard never repairs REJECT or MISSING results.",
        }

    if result.get("format") == "audiobook":
        if not settings.repair_audiobooks or not settings.repair_normalize_pass:
            return {"eligible": False, "safe": False, "kind": "", "reason": "Audiobook repair is disabled."}
        if result.get("classification") != "PASS" or not _audio_summary_safe(result):
            return {
                "eligible": False,
                "safe": False,
                "kind": "",
                "reason": "Audiobook identity is not strong enough for safe metadata repair.",
            }
        metadata = result.get("metadata") or {}
        current_title = str(metadata.get("detected_title") or "")
        current_author = str(metadata.get("detected_author") or "")
        author_order_change = (
            settings.repair_audio_album_artist
            and current_author
            and _same_author_person(result["author"], current_author)
            and normalize(current_author) != normalize(result["author"])
        )
        needs_change = (
            (settings.repair_audio_album and not _same_text(current_title, result["title"]))
            or author_order_change
            or settings.repair_audio_genre
        )
        return {
            "eligible": needs_change,
            "safe": needs_change,
            "kind": "AUDIO_TAGS",
            "reason": "Conservatively normalize exact-title audiobook metadata; valid aliases are preserved." if needs_change else "No conservative audio metadata changes are needed.",
        }

    if not settings.repair_ebooks:
        return {"eligible": False, "safe": False, "kind": "", "reason": "Ebook repair is disabled."}

    target = _resolve_ebook_target(result.get("local_path") or "")
    if Path(target).suffix.lower() != ".epub":
        return {
            "eligible": False,
            "safe": False,
            "kind": "",
            "reason": "Writing is currently limited to EPUB for ebooks; other formats stay preview-only.",
        }

    metadata = result.get("metadata") or {}
    current_title = str(metadata.get("title") or "")
    current_author = str(metadata.get("author") or "")
    if result.get("reason_code") == "SWAPPED_METADATA":
        return {
            "eligible": True,
            "safe": True,
            "kind": "EPUB_METADATA",
            "reason": "Correct confirmed swapped EPUB title/author fields.",
        }

    if result.get("classification") == "PASS" and settings.repair_normalize_pass:
        safe_identity = _repair_title_equivalent(result["title"], current_title) and author_match_strict(
            result["author"], current_author
        )
        author_order_change = (
            safe_identity
            and _same_author_person(result["author"], current_author)
            and normalize(current_author) != normalize(result["author"])
        )
        if author_order_change:
            return {
                "eligible": True,
                "safe": True,
                "kind": "EPUB_METADATA",
                "reason": "Normalize confirmed EPUB author-name ordering; title variants and pen-name credits are preserved.",
            }

    return {
        "eligible": False,
        "safe": False,
        "kind": "",
        "reason": "The result does not meet BookGuard's conservative repair rules.",
    }


def _read_audio_fields(path: str) -> dict:
    media = MutagenFile(path, easy=True)
    if media is None:
        raise RepairError(f"Mutagen could not identify {path}")
    if media.tags is None:
        media.add_tags()
    tags = media.tags or {}
    return {
        "album": _first(tags.get("album")),
        "albumartist": _first(tags.get("albumartist")),
        "genre": _first(tags.get("genre")),
    }


def _write_audio_fields(path: str, values: dict) -> None:
    media = MutagenFile(path, easy=True)
    if media is None:
        raise RepairError(f"Mutagen could not identify {path}")
    if media.tags is None:
        media.add_tags()
    for key in ("album", "albumartist", "genre"):
        if key not in values:
            continue
        value = str(values.get(key) or "")
        try:
            if value:
                media[key] = [value]
            elif key in media:
                del media[key]
        except Exception as exc:
            raise RepairError(f"Unable to write {key} in {path}: {exc}") from exc
    media.save()


def _audio_preview(result: dict) -> dict:
    paths = _all_audio_paths(result["local_path"])
    if not paths:
        raise RepairError("No audio files were found under the tracked audiobook path.")
    unsupported = [path for path in paths if Path(path).suffix.lower() not in WRITABLE_AUDIO_EXTENSIONS]
    if unsupported:
        return {
            "eligible": False,
            "safe": False,
            "kind": "AUDIO_TAGS",
            "reason": "The audiobook contains audio formats BookGuard does not safely write yet.",
            "unsupported_files": unsupported,
            "before": {},
            "after": {},
        }

    before_files: list[dict] = []
    after_files: list[dict] = []
    changed = 0
    for path in paths:
        before = _read_audio_fields(path)
        after = dict(before)
        if settings.repair_audio_album and not _same_text(before.get("album"), result["title"]):
            after["album"] = result["title"]
        if (
            settings.repair_audio_album_artist
            and _same_author_person(result["author"], before.get("albumartist"))
            and normalize(before.get("albumartist")) != normalize(result["author"])
        ):
            after["albumartist"] = result["author"]
        if settings.repair_audio_genre:
            after["genre"] = settings.repair_audio_genre_value or "Audiobook"
        if before != after:
            changed += 1
        before_files.append({"path": path, "tags": before})
        after_files.append({"path": path, "tags": after})

    return {
        "eligible": changed > 0,
        "safe": changed > 0,
        "kind": "AUDIO_TAGS",
        "reason": f"{changed} of {len(paths)} audio file(s) would receive conservative book-level tag cleanup.",
        "before": {"files": before_files},
        "after": {"files": after_files},
    }


def _epub_package(path: str) -> tuple[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = ""
        for elem in container.iter():
            if elem.tag.endswith("rootfile"):
                rootfile = elem.attrib.get("full-path", "")
                if rootfile:
                    break
        if not rootfile:
            raise RepairError("EPUB container does not declare an OPF package.")
        return rootfile, zf.read(rootfile)


def _rewrite_epub_metadata(path: str, title: str, author: str) -> None:
    rootfile, package_bytes = _epub_package(path)
    package = ET.fromstring(package_bytes)
    metadata_node = None
    title_node = None
    creator_node = None
    for elem in package.iter():
        local = elem.tag.rsplit("}", 1)[-1].lower()
        if local == "metadata" and metadata_node is None:
            metadata_node = elem
        elif local == "title" and title_node is None:
            title_node = elem
        elif local == "creator" and creator_node is None:
            creator_node = elem

    if metadata_node is None:
        raise RepairError("EPUB OPF package does not contain a metadata element.")

    dc_ns = "http://purl.org/dc/elements/1.1/"
    ET.register_namespace("dc", dc_ns)
    if title_node is None:
        title_node = ET.SubElement(metadata_node, f"{{{dc_ns}}}title")
    if creator_node is None:
        creator_node = ET.SubElement(metadata_node, f"{{{dc_ns}}}creator")
    title_node.text = title
    creator_node.text = author
    replacement = ET.tostring(package, encoding="utf-8", xml_declaration=True)

    source = Path(path)
    fd, temp_name = tempfile.mkstemp(prefix=f".{source.name}.bookguard-", suffix=".tmp", dir=source.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(source, "r") as src, zipfile.ZipFile(temp_name, "w") as dst:
            for info in src.infolist():
                payload = replacement if info.filename == rootfile else src.read(info.filename)
                dst.writestr(info, payload)
        shutil.copystat(source, temp_name)
        os.replace(temp_name, source)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _epub_preview(result: dict) -> dict:
    target = _resolve_ebook_target(result["local_path"])
    if Path(target).suffix.lower() != ".epub":
        return {
            "eligible": False,
            "safe": False,
            "kind": "EPUB_METADATA",
            "reason": "BookGuard currently writes EPUB metadata only.",
            "before": {},
            "after": {},
        }
    current = ebook_metadata(target)
    before = {
        "path": target,
        "title": str(current.get("title") or ""),
        "author": str(current.get("author") or ""),
    }
    if result.get("reason_code") == "SWAPPED_METADATA":
        after = {"path": target, "title": result["title"], "author": result["author"]}
    else:
        after = dict(before)
        if (
            _same_author_person(result["author"], before["author"])
            and normalize(before["author"]) != normalize(result["author"])
        ):
            after["author"] = result["author"]
    return {
        "eligible": before != after,
        "safe": before != after,
        "kind": "EPUB_METADATA",
        "reason": "Rewrite only the confirmed defective EPUB metadata fields.",
        "before": before,
        "after": after,
    }


def build_repair_preview(result: dict) -> dict:
    summary = repair_candidate_summary(result)
    if not summary.get("eligible") or not summary.get("safe"):
        return {**summary, "before": {}, "after": {}}
    if result["format"] == "audiobook":
        return _audio_preview(result)
    return _epub_preview(result)


def apply_repair_changes(preview: dict) -> None:
    """Apply the changes from a preview already approved by a repair safety gate."""
    kind = preview["kind"]
    if kind == "EPUB_METADATA":
        after = preview["after"]
        _rewrite_epub_metadata(after["path"], after["title"], after["author"])
        return
    if kind == "AUDIO_TAGS":
        written: list[dict] = []
        before_lookup = {item["path"]: item["tags"] for item in preview["before"]["files"]}
        try:
            for item in preview["after"]["files"]:
                _write_audio_fields(item["path"], item["tags"])
                written.append(item)
        except Exception:
            for item in reversed(written):
                try:
                    _write_audio_fields(item["path"], before_lookup[item["path"]])
                except Exception:
                    pass
            raise
        return
    raise RepairError(f"Unsupported repair kind: {kind}")


def verify_repair_changes(preview: dict) -> None:
    """Confirm that the current media metadata matches an approved repair preview."""
    if preview["kind"] == "EPUB_METADATA":
        after = preview["after"]
        current = ebook_metadata(after["path"])
        if not _same_text(current.get("title"), after["title"]) or normalize(current.get("author")) != normalize(after["author"]):
            raise RepairError("EPUB metadata verification failed after writing.")
        return
    if preview["kind"] == "AUDIO_TAGS":
        for item in preview["after"]["files"]:
            current = _read_audio_fields(item["path"])
            expected = item["tags"]
            for key in expected:
                if normalize(current.get(key)) != normalize(expected.get(key)):
                    raise RepairError(f"Audio metadata verification failed for {item['path']} ({key}).")
        return


def apply_metadata_repair(result: dict) -> dict:
    require_current_scan_result(result)
    if settings.metadata_repair_mode != "safe":
        raise RepairError(
            "Metadata repair is not in Safe mode. Use Preview mode to inspect proposals without writing files."
        )
    preview = build_repair_preview(result)
    if not preview.get("eligible") or not preview.get("safe"):
        raise RepairError(preview.get("reason") or "This result is not eligible for safe metadata repair.")

    all_paths: list[str]
    if preview["kind"] == "AUDIO_TAGS":
        all_paths = [item["path"] for item in preview["after"]["files"]]
    else:
        all_paths = [preview["after"]["path"]]
    unwritable = [path for path in all_paths if not os.access(path, os.W_OK)]
    if unwritable:
        raise RepairError(
            "The media mount is read-only or not writable. Keep Preview mode until you deliberately make the relevant Docker media mount writable."
        )

    require_current_scan_result(result)
    repair_id = create_metadata_repair(result, preview["kind"], preview["before"], preview["after"])
    try:
        apply_repair_changes(preview)
        verify_repair_changes(preview)
    except Exception as exc:
        finish_metadata_repair(repair_id, "failed", str(exc)[:1000])
        raise RepairError(str(exc)) from exc
    finish_metadata_repair(repair_id, "applied")
    return {"repair_id": repair_id, **preview}


def undo_metadata_repair(repair_id: int) -> dict:
    repair = metadata_repair_by_id(repair_id)
    if not repair:
        raise RepairError("Repair history entry not found.")
    if repair["status"] != "applied":
        raise RepairError(f"Only applied repairs can be undone; current status is {repair['status']}.")
    if settings.metadata_repair_mode != "safe":
        raise RepairError("Switch metadata repair mode to Safe before undoing a repair.")

    preview = {
        "kind": repair["repair_kind"],
        "before": repair["after"],
        "after": repair["before"],
    }
    # Refuse to overwrite later external changes: the current metadata must still
    # match exactly what BookGuard wrote before an undo is allowed.
    verify_repair_changes({"kind": repair["repair_kind"], "after": repair["after"]})
    apply_repair_changes(preview)
    verify_repair_changes({"kind": repair["repair_kind"], "after": repair["before"]})
    mark_metadata_repair_undone(repair_id)
    return {"ok": True, "repair_id": repair_id}

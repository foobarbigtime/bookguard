"""Read-only preview of the separate works inside a mixed audiobook folder.

A folder flagged MIXED_AUDIO_CONTENT holds recordings of several works under one
Bindery assignment. This module groups the folder's audio files by the work
identity they declare, matches each group to the Bindery catalogue, and reports
the split a person would need to make. It never moves, copies, or deletes
anything and never calls Bindery: applying a split is a separate, later step.
"""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import re
from typing import Any

from .audiobook_verification import cached_or_probe_audio_file
from .matcher import (
    _audio_main_title,
    audio_title_supported,
    audio_work_identity,
    author_match,
    normalize,
)
from .media_discovery import discover_audio_files

MAX_FILES = 2000


# Release-set labels that split one work into many albums: "… Disc 12", "… CD 3",
# and a leading track-code such as "L17-08 ".
_DISC_SUFFIX = re.compile(r"[\s:,\-_(\[]*\b(?:disc|disk|cd)\s*\d+\s*(?:of\s*\d+)?[)\]]*\s*$", re.IGNORECASE)
_CODE_PREFIX = re.compile(r"^\s*[A-Za-z]{1,3}\d+[-_.]\d+\s+")


def _work_title(title: str) -> str:
    """The work a track names, without disc numbers or release codes."""
    head = _audio_main_title(title) or title
    head = _CODE_PREFIX.sub("", head)
    return _DISC_SUFFIX.sub("", head).strip() or head.strip()


def _group_key(title: str) -> str:
    # Spaces are dropped so "HarryPotter" and "Harry Potter" are the same work.
    return normalize(_work_title(title)).replace(" ", "")


def _track_order(probe: dict[str, Any]) -> tuple[int, int, str]:
    def number(value: Any) -> int:
        head = str(value or "").split("/")[0].strip()
        return int(head) if head.isdigit() else 0

    return number(probe.get("disc")), number(probe.get("track")), str(probe.get("path") or "").casefold()


def _catalogue_match(library: Any, title: str, author: str) -> dict[str, Any]:
    book, candidates = library.find(title, author)
    if book:
        return {"status": "catalogue", "book": {"id": int(book["id"]), "title": str(book["title"]),
                                                 "author": str(book["author"])}}
    if candidates:
        return {"status": "ambiguous", "candidates": [
            {"id": int(b["id"]), "title": str(b["title"]), "author": str(b["author"])}
            for b in candidates[:5]]}
    return {"status": "not_in_catalogue"}


def _surnames(credit: str) -> set[str]:
    """Last names of each person in a credit such as "James Patterson, Marshall Karp"."""
    names = set()
    for part in re.split(r"\s*(?:,|;|&|/|\band\b)\s*", credit or ""):
        words = normalize(part).split()
        if words:
            names.add(words[-1])
    return names


def _same_author(a: str, b: str) -> bool:
    """Tolerant to tag typos ("Rowing"/"Rowling") and co-author lists, not to different people."""
    return any(x == y or SequenceMatcher(None, x, y).ratio() >= 0.85
               for x in _surnames(a) for y in _surnames(b))


def _author_clusters(credits: Counter) -> list[list[str]]:
    """Group spellings of the same credited author, most common spelling first."""
    clusters: list[list[str]] = []
    for credit, _ in credits.most_common():
        for cluster in clusters:
            if _same_author(credit, cluster[0]):
                cluster.append(credit)
                break
        else:
            clusters.append([credit])
    return clusters


def group_probes(item: dict[str, Any], probes: list[dict[str, Any]], library: Any) -> dict[str, Any]:
    """Group probed files by declared work and match each group to the catalogue."""
    expected_title = str(item.get("title") or "")
    expected_book_id = int(item.get("book_id") or 0)
    expected_author = str(item.get("author") or "")
    by_title: dict[str, dict[str, Any]] = {}
    unreadable: list[str] = []
    unidentified: list[dict[str, Any]] = []

    for probe in sorted(probes, key=_track_order):
        if probe.get("probe_error"):
            unreadable.append(str(probe.get("path") or ""))
            continue
        title, author = audio_work_identity(probe)
        key = _group_key(title) if title else ""
        if not key:
            unidentified.append(probe)
            continue
        bucket = by_title.setdefault(key, {"names": Counter(), "entries": []})
        bucket["names"][_work_title(title)] += 1
        bucket["entries"].append((probe, author))

    # Works that share a title but declare different authors are different works.
    buckets: list[dict[str, Any]] = []
    for bucket in by_title.values():
        title = bucket["names"].most_common(1)[0][0]
        credits = Counter(author for _, author in bucket["entries"] if author)
        clusters = _author_clusters(credits) or [[""]]
        parts: list[dict[str, Any]] = [{"title": title, "credits": Counter(), "files": []} for _ in clusters]
        for probe, author in bucket["entries"]:
            index = next((i for i, cluster in enumerate(clusters) if author in cluster), 0)
            parts[index]["files"].append(probe)
            if author:
                parts[index]["credits"][author] += 1
        buckets += parts

    groups = []
    for bucket in buckets:
        credits = bucket["credits"]
        author = credits.most_common(1)[0][0] if credits else ""
        match = _catalogue_match(library, bucket["title"], author)
        is_assigned = audio_title_supported(expected_title, {"album": bucket["title"]})
        if is_assigned and author and expected_author:
            is_assigned = author_match(expected_author, author) or _same_author(expected_author, author)
        if match["status"] == "catalogue" and expected_book_id and match["book"]["id"] == expected_book_id:
            is_assigned = True
        groups.append({
            "title": bucket["title"],
            "author": author,
            "fileCount": len(bucket["files"]),
            "files": [str(p.get("path") or "") for p in bucket["files"]],
            "assigned": is_assigned,
            **match,
        })
    groups.sort(key=lambda g: (not g["assigned"], -g["fileCount"], g["title"].casefold()))

    other_works = [g for g in groups if not g["assigned"]]
    return {
        "groups": groups,
        "unidentified": [str(p.get("path") or "") for p in unidentified],
        "unreadable": unreadable,
        "fileCount": len(probes),
        "workCount": len(groups),
        "splitNeeded": bool(other_works),
        "applicable": False,
    }


def preview_recording_groups(item: dict[str, Any], *, library: Any | None = None) -> dict[str, Any]:
    """Read-only split preview for one audiobook scan result."""
    if item.get("format") != "audiobook":
        raise ValueError("Recording groups apply to audiobooks only.")
    target = str(item.get("local_path") or "")
    files = discover_audio_files(target) if target else []
    if not files:
        raise ValueError("No audio files were found at this path.")
    if len(files) > MAX_FILES:
        raise ValueError(f"This folder has {len(files)} audio files; the preview reads at most {MAX_FILES}.")
    if library is None:
        from .unmatched import Library, library_books
        library = Library(library_books())
    probes = [cached_or_probe_audio_file(path)[0] for path in files]
    preview = group_probes(item, probes, library)
    preview["resultId"] = int(item.get("id") or 0)
    preview["note"] = ("Preview only. BookGuard has not moved, copied, or changed anything, and nothing "
                       "in Bindery. Splitting a folder is a separate step.")
    return preview

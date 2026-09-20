from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from app.audiobook_verification import verify_audiobook
from app.config import settings
from app.db import load_bindery_files


def map_audiobook_path(stored_path: str) -> str:
    prefix = settings.audiobook_bindery_prefix
    root = settings.audiobook_root
    if stored_path == prefix:
        return root
    if stored_path.startswith(prefix + "/"):
        return os.path.join(root, stored_path[len(prefix) + 1 :])
    return stored_path


def representative_rows(rows: list[dict], limit: int) -> list[dict]:
    if not rows or limit <= 0:
        return []
    if len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[len(rows) // 2]]

    indexes = [round(i * (len(rows) - 1) / (limit - 1)) for i in range(limit)]
    selected: list[dict] = []
    seen: set[int] = set()
    for index in indexes:
        if index not in seen:
            seen.add(index)
            selected.append(rows[index])
    return selected


def _progress_callback(book_index: int, book_total: int, title: str):
    def report(event: dict) -> None:
        if event.get("phase") != "probing":
            return
        file_index = int(event.get("file_index") or 0)
        file_total = int(event.get("file_total") or 0)
        path = str(event.get("path") or "")
        filename = Path(path).name if path else ""
        prefix = f"[{book_index}/{book_total}] {title}"

        if sys.stderr.isatty():
            message = f"{prefix} — audio file {file_index}/{file_total}"
            if filename:
                message += f" — {filename}"
            print(f"\r{message[:180]:<180}", end="", file=sys.stderr, flush=True)
            return

        # Non-interactive output is intentionally throttled so large books do not
        # flood logs while still proving that the scan is making progress.
        if file_index == 1 or file_index == file_total or file_index % 10 == 0:
            message = f"{prefix} — audio file {file_index}/{file_total}"
            if filename:
                message += f" — {filename}"
            print(message, file=sys.stderr, flush=True)

    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only technical verification of a representative audiobook sample."
    )
    parser.add_argument("--limit", type=int, default=5, help="Number of audiobooks to sample.")
    args = parser.parse_args()

    rows = [row for row in load_bindery_files() if row.get("format") == "audiobook"]
    sample = representative_rows(rows, max(1, min(args.limit, 25)))

    print(
        (
            f"Starting read-only verification of {len(sample)} audiobook(s). "
            "This may take a while because every discovered audio file is checked."
        ),
        file=sys.stderr,
        flush=True,
    )
    print("Live progress will be shown below.", file=sys.stderr, flush=True)

    output: list[dict] = []
    for index, row in enumerate(sample, start=1):
        local_path = map_audiobook_path(str(row.get("stored_path") or ""))
        title = str(row.get("title") or "Untitled")
        author = str(row.get("author") or "Unknown author")
        print(
            f"[{index}/{len(sample)}] Starting: {author} — {title}",
            file=sys.stderr,
            flush=True,
        )
        verification = verify_audiobook(
            local_path,
            progress_callback=_progress_callback(index, len(sample), title),
        )
        if sys.stderr.isatty():
            print(file=sys.stderr)
        print(
            (
                f"[{index}/{len(sample)}] Finished: {verification.get('verdict')} — "
                f"{verification.get('readable_file_count', 0)}/"
                f"{verification.get('file_count', 0)} readable file(s)"
            ),
            file=sys.stderr,
            flush=True,
        )
        output.append(
            {
                "book_id": row.get("book_id"),
                "author": row.get("author"),
                "title": row.get("title"),
                "stored_path": row.get("stored_path"),
                "local_path": local_path,
                "verdict": verification.get("verdict"),
                "reason_code": verification.get("reason_code"),
                "file_count": verification.get("file_count"),
                "readable_file_count": verification.get("readable_file_count"),
                "total_duration_seconds": verification.get("total_duration_seconds"),
                "codecs": verification.get("codecs", []),
                "sample_rates": verification.get("sample_rates", []),
                "channels": verification.get("channels", []),
                "chapter_count": verification.get("chapter_count", 0),
                "reasons": verification.get("reasons", []),
            }
        )

    print(
        json.dumps(
            {
                "mode": "read_only",
                "available_audiobook_rows": len(rows),
                "sample_size": len(output),
                "results": output,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

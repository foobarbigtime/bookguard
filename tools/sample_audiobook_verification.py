from __future__ import annotations

import argparse
import json
import os

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only technical verification of a representative audiobook sample."
    )
    parser.add_argument("--limit", type=int, default=5, help="Number of audiobooks to sample.")
    args = parser.parse_args()

    rows = [row for row in load_bindery_files() if row.get("format") == "audiobook"]
    sample = representative_rows(rows, max(1, min(args.limit, 25)))

    output: list[dict] = []
    for row in sample:
        local_path = map_audiobook_path(str(row.get("stored_path") or ""))
        verification = verify_audiobook(local_path)
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

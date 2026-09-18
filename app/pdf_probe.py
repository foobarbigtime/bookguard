from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


MAX_ROUTINE_PDF_BYTES = 512 * 1024 * 1024
PDF_PROBE_TIMEOUT_SECONDS = 10
PDF_CONTENT_TIMEOUT_SECONDS = 30
PDF_PROBE_ADDRESS_SPACE_BYTES = 768 * 1024 * 1024
PDF_PROBE_CPU_SECONDS = 5
PDF_CONTENT_CPU_SECONDS = 20
MAX_WORKER_OUTPUT_BYTES = 64 * 1024
MAX_CONTENT_TEXT_CHARS = 5_000_000
MAX_CONTENT_PAGES = 100
MAX_CONTENT_FRONT_CHARS = 200_000


def _preflight(path: Path) -> None:
    if not path.is_file():
        raise ValueError("PDF path is not a regular file.")

    size = path.stat().st_size
    if size > MAX_ROUTINE_PDF_BYTES:
        raise ValueError(
            f"PDF exceeds the {MAX_ROUTINE_PDF_BYTES}-byte routine metadata limit."
        )

    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValueError("PDF header is absent.")
        handle.seek(max(0, size - 4096))
        trailer = handle.read(4096)
    if b"%%EOF" not in trailer:
        raise ValueError("PDF end-of-file marker is absent.")


@lru_cache(maxsize=128)
def _probe_cached(
    path_text: str,
    size: int,
    modified_ns: int,
    changed_ns: int,
    device: int,
    inode: int,
) -> dict[str, Any]:
    del size, modified_ns, changed_ns, device, inode
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.pdf_probe", "--worker", path_text],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=PDF_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "PDF metadata probe timed out."}

    if proc.returncode != 0:
        return {"error": "PDF metadata probe failed in the isolated worker."}
    if len(proc.stdout.encode("utf-8", errors="replace")) > MAX_WORKER_OUTPUT_BYTES:
        return {"error": "PDF metadata probe returned too much output."}

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "PDF metadata probe returned invalid JSON."}
    if not isinstance(payload, dict):
        return {"error": "PDF metadata probe returned an invalid result."}
    return payload


def probe_pdf(path: str | Path) -> dict[str, Any]:
    """Read routine PDF metadata in a time- and memory-bounded child process."""
    target = Path(path)
    try:
        _preflight(target)
        stat = target.stat()
    except (OSError, ValueError) as exc:
        return {"error": str(exc)[:500]}

    return dict(
        _probe_cached(
            str(target),
            int(stat.st_size),
            int(stat.st_mtime_ns),
            int(stat.st_ctime_ns),
            int(stat.st_dev),
            int(stat.st_ino),
        )
    )


def extract_pdf_text(
    path: str | Path,
    *,
    max_chars: int,
    page_limit: int,
    front_chars: int,
) -> dict[str, Any]:
    """Extract bounded PDF identity text in an isolated child process."""
    target = Path(path)
    try:
        _preflight(target)
        max_chars = int(max_chars)
        page_limit = int(page_limit)
        front_chars = int(front_chars)
    except (OSError, TypeError, ValueError) as exc:
        return {"error": str(exc)[:500]}

    if not (1 <= max_chars <= MAX_CONTENT_TEXT_CHARS):
        return {"error": "PDF content text limit is outside the allowed range."}
    if not (1 <= page_limit <= MAX_CONTENT_PAGES):
        return {"error": "PDF content page limit is outside the allowed range."}
    if not (1 <= front_chars <= MAX_CONTENT_FRONT_CHARS):
        return {"error": "PDF front-text limit is outside the allowed range."}

    output_limit = (max_chars + front_chars) * 8 + MAX_WORKER_OUTPUT_BYTES
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.pdf_probe",
                "--extract-worker",
                str(target),
                str(max_chars),
                str(page_limit),
                str(front_chars),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=PDF_CONTENT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "PDF content extraction timed out."}

    if proc.returncode != 0:
        return {"error": "PDF content extraction failed in the isolated worker."}
    if len(proc.stdout.encode("utf-8", errors="replace")) > output_limit:
        return {"error": "PDF content extraction returned too much output."}

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "PDF content extraction returned invalid JSON."}
    if not isinstance(payload, dict):
        return {"error": "PDF content extraction returned an invalid result."}
    return payload


def _apply_worker_limits(cpu_seconds: int = PDF_PROBE_CPU_SECONDS) -> None:
    import resource

    resource.setrlimit(
        resource.RLIMIT_AS,
        (PDF_PROBE_ADDRESS_SPACE_BYTES, PDF_PROBE_ADDRESS_SPACE_BYTES),
    )
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (cpu_seconds, cpu_seconds),
    )


def _metadata_worker(path: str) -> int:
    try:
        _apply_worker_limits()
        from pypdf import PdfReader

        reader = PdfReader(path, strict=False)
        metadata = reader.metadata or {}
        root = reader.trailer.get("/Root")
        language = str(root.get("/Lang") or "").strip() if root else ""
        payload = {
            "title": str(metadata.get("/Title") or "").strip(),
            "author": str(metadata.get("/Author") or "").strip(),
            "language": language,
        }
    except Exception as exc:
        payload = {"error": str(exc)[:500]}

    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


def _content_worker(
    path: str,
    max_chars: int,
    page_limit: int,
    front_chars: int,
) -> int:
    try:
        _apply_worker_limits(PDF_CONTENT_CPU_SECONDS)
        from pypdf import PdfReader

        reader = PdfReader(path, strict=False)
        metadata = reader.metadata or {}
        parts: list[str] = []
        front_parts: list[str] = []
        total = 0
        front_total = 0
        pages = min(len(reader.pages), page_limit)

        for index in range(pages):
            if total >= max_chars:
                break
            try:
                part = reader.pages[index].extract_text() or ""
            except Exception:
                continue
            if not part:
                continue

            remaining = max_chars - total
            text_part = part[:remaining]
            parts.append(text_part)
            total += len(text_part)

            if index < 6 and front_total < front_chars:
                front_remaining = front_chars - front_total
                front_part = part[:front_remaining]
                front_parts.append(front_part)
                front_total += len(front_part)

        payload = {
            "title": str(metadata.get("/Title") or "").strip(),
            "author": str(metadata.get("/Author") or "").strip(),
            "text": " ".join(parts)[:max_chars],
            "front_text": " ".join(front_parts)[:front_chars],
        }
    except Exception as exc:
        payload = {"error": str(exc)[:500]}

    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(_metadata_worker(sys.argv[2]))
    if len(sys.argv) == 6 and sys.argv[1] == "--extract-worker":
        raise SystemExit(
            _content_worker(
                sys.argv[2],
                int(sys.argv[3]),
                int(sys.argv[4]),
                int(sys.argv[5]),
            )
        )
    raise SystemExit(2)

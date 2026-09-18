from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


MAX_ROUTINE_PDF_BYTES = 512 * 1024 * 1024
PDF_PROBE_TIMEOUT_SECONDS = 10
PDF_PROBE_ADDRESS_SPACE_BYTES = 768 * 1024 * 1024
PDF_PROBE_CPU_SECONDS = 5
MAX_WORKER_OUTPUT_BYTES = 64 * 1024


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


def _apply_worker_limits() -> None:
    import resource

    resource.setrlimit(
        resource.RLIMIT_AS,
        (PDF_PROBE_ADDRESS_SPACE_BYTES, PDF_PROBE_ADDRESS_SPACE_BYTES),
    )
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (PDF_PROBE_CPU_SECONDS, PDF_PROBE_CPU_SECONDS),
    )


def _worker(path: str) -> int:
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


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    raise SystemExit(_worker(sys.argv[2]))

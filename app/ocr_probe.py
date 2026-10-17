"""OCR of the first pages of a PDF that has no text layer (a scan), in bounded child processes.

An image-only PDF yields no text, so nothing about it can be proven from its content. The
title page, however, is a picture of exactly the evidence needed. This module renders the first
few pages with Poppler's ``pdftoppm`` and reads them with Tesseract, so the normal verification
can look for the title and author on the page.

Safety, in the same spirit as ``pdf_probe``: the PDF it is given is the verifier's private
snapshot; every child process has a wall-clock timeout, a bounded output, and CPU and
address-space limits; temporary images live in a private directory that is always removed; the
number of pages and the amount of text are small; and nothing is written anywhere else.
Pages stay separated by a form feed, and pages that list other books are set aside.
OCR can misread a name. That can only make a title or author go unfound (the book stays
undecided), never match something that is not there.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from .config import settings
from .pdf_probe import _preflight
from .scan_text import PAGE_BREAK, without_book_lists
from .subprocess_capture import ProcessOutputLimitExceeded, run_bounded_process

OCR_POLICY_VERSION = 1             # Version of captured OCR settings, independent of evidence-reader rules.
RENDER_MAX_PIXELS = 2400           # longest side of a rendered page: bounds the work whatever the page size
RENDER_TIMEOUT_SECONDS = 45
PAGE_OCR_TIMEOUT_SECONDS = 60
RENDER_CPU_SECONDS = 40
OCR_CPU_SECONDS = 50
RENDER_ADDRESS_SPACE_BYTES = 2 * 1024 ** 3
OCR_ADDRESS_SPACE_BYTES = 3 * 1024 ** 3
MAX_PAGE_FILE_BYTES = 40 * 1024 * 1024
MAX_PAGE_TEXT_BYTES = 256 * 1024
MAX_TEXT_CHARS = 20_000
ENOUGH_TEXT_CHARS = 4_000          # stop early once the title page has been read
_LANGUAGES = re.compile(r"^[a-z]{3}(?:_[a-z]{2,8})?(?:\+[a-z]{3}(?:_[a-z]{2,8})?)*$")


def _limited(command: list[str], cpu_seconds: int, address_space: int) -> list[str]:
    """The command wrapped in util-linux ``prlimit`` (CPU, memory, output-file size, no core dump).

    ``prlimit`` is used instead of a ``preexec_fn`` because the server is multi-threaded and a
    Python pre-exec hook is not safe there. Without ``prlimit`` the wall-clock timeout and the
    output limits of ``run_bounded_process`` still apply."""
    prlimit = shutil.which("prlimit")
    if not prlimit:
        return command
    return [prlimit, f"--cpu={cpu_seconds}", f"--as={address_space}", f"--fsize={MAX_PAGE_FILE_BYTES}",
            "--core=0", "--", *command]


def _languages() -> str:
    value = str(settings.verification_ocr_languages or "").strip().lower()
    return value if _LANGUAGES.fullmatch(value) else "eng"


def ocr_policy() -> dict[str, Any]:
    """OCR settings captured for one attempt; unlike general verification settings they do not rekey all ebooks."""
    return {"version": OCR_POLICY_VERSION, "enabled": bool(settings.verification_ocr),
            "pages": int(settings.verification_ocr_pages), "languages": _languages()}


def _child_env(home: str) -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OMP_THREAD_LIMIT": "1",
            "LC_ALL": "C", "HOME": home}


def ocr_pdf_front(path: str | Path) -> dict[str, Any]:
    """OCR text of the first pages: {"text", "pages"} on success, {"error"} or {"disabled"} otherwise."""
    policy = ocr_policy()
    if not policy["enabled"]:
        return {"disabled": True}
    pdftoppm, tesseract = shutil.which("pdftoppm"), shutil.which("tesseract")
    if not pdftoppm or not tesseract:
        return {"error": "OCR is not available in this build (Poppler or Tesseract is not installed).",
                "error_code": "dependency_unavailable", "policy": policy}
    target = Path(path)
    try:
        _preflight(target)
        source = str(target.resolve(strict=True))
    except ValueError as exc:
        return {"error": f"OCR skipped: {str(exc)[:200]}", "error_code": "invalid_pdf", "policy": policy}
    except OSError as exc:
        return {"error": f"OCR skipped: {str(exc)[:200]}", "error_code": "preflight_io_error", "policy": policy}

    pages = policy["pages"]
    with tempfile.TemporaryDirectory(prefix="bookguard-ocr-") as work:
        try:
            rendered = run_bounded_process(
                _limited([pdftoppm, "-f", "1", "-l", str(pages), "-scale-to", str(RENDER_MAX_PIXELS), "-gray", "-png", source,
                          str(Path(work) / "pg")], RENDER_CPU_SECONDS, RENDER_ADDRESS_SPACE_BYTES),
                timeout=RENDER_TIMEOUT_SECONDS, stdout_limit=64 * 1024, stderr_limit=64 * 1024,
                env=_child_env(work),
            )
        except subprocess.TimeoutExpired:
            return {"error": "OCR skipped: rendering the first pages timed out.",
                    "error_code": "render_timeout", "policy": policy}
        except ProcessOutputLimitExceeded:
            return {"error": "OCR skipped: rendering exceeded its output limit.",
                    "error_code": "render_output_limit", "policy": policy}
        except OSError:
            return {"error": "OCR skipped: the rendering process could not start.",
                    "error_code": "render_execution_error", "policy": policy}
        images = sorted(Path(work).glob("pg-*.png"))[:pages]
        if rendered.returncode != 0 or not images:
            stderr = rendered.stderr.decode("utf-8", errors="replace").lower()
            # Only explicit Poppler password diagnostics are known to be persistent.
            protected = any(message in stderr for message in ("incorrect password", "password required"))
            return {"error": "OCR skipped: the PDF requires a password." if protected
                    else "OCR skipped: the first pages could not be rendered.",
                    "error_code": "password_protected" if protected else "render_error", "policy": policy}

        chunks: list[str] = []
        read = 0
        failed = 0
        for image in images:
            try:
                result = run_bounded_process(
                    _limited([tesseract, str(image), "stdout", "-l", policy["languages"], "--psm", "3"],
                             OCR_CPU_SECONDS, OCR_ADDRESS_SPACE_BYTES),
                    timeout=PAGE_OCR_TIMEOUT_SECONDS, stdout_limit=MAX_PAGE_TEXT_BYTES, stderr_limit=64 * 1024,
                    env=_child_env(work),
                )
            except (subprocess.TimeoutExpired, ProcessOutputLimitExceeded, OSError):
                failed += 1
                continue
            if result.returncode != 0:
                failed += 1
                continue
            read += 1
            chunks.append(re.sub(r"[ \t]+", " ", result.stdout.decode("utf-8", errors="replace")).strip())
            # Count only what will be kept: a page of praise or of other titles is set aside.
            if len(without_book_lists(PAGE_BREAK.join(c for c in chunks if c))[0]) >= ENOUGH_TEXT_CHARS:
                break
    raw = PAGE_BREAK.join(c for c in chunks if c)
    if not raw.strip():
        return {"error": f"OCR read {read} page image(s) but found no text.", "pages": read,
                "error_code": "ocr_execution_error" if failed else "no_text", "policy": policy}
    # Pages that list other books (or praise, or the author's bio) are not the book's title page.
    text, set_aside = without_book_lists(raw)
    text = text[:MAX_TEXT_CHARS]
    if not text.strip():
        return {"error": f"OCR read {read} page(s), but they only list other books (\"Also by ...\"), so none "
                         "is a title page.", "pages": read, "setAside": set_aside,
                "error_code": "ocr_execution_error" if failed else "no_title_page", "policy": policy}
    found = {"text": text, "pages": read, "setAside": set_aside, "policy": policy}
    if failed:
        found.update(error="OCR could not read every rendered page image.",
                     error_code="ocr_execution_error")
    return found

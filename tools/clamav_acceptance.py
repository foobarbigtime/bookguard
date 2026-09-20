from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import time

from app.ebook_security import inspect_ebook_security
from app.malware_scan import probe_clamd, scan_with_clamd


def _eicar_bytes() -> bytes:
    # Construct the harmless EICAR antivirus test pattern at runtime so the
    # repository itself does not contain the complete signature as one literal.
    return (
        b"X5O!P%@AP[4"
        + bytes((92,))
        + b"PZX54(P^)7CC)7}$"
        + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    )


def _wait_for_clamd(host: str, port: int, wait_seconds: int) -> dict:
    deadline = time.monotonic() + max(1, wait_seconds)
    last = {"ok": False, "message": "ClamAV has not been probed yet."}
    while True:
        last = probe_clamd(host=host, port=port, timeout_seconds=5)
        if last.get("ok"):
            return last
        if time.monotonic() >= deadline:
            raise RuntimeError(last.get("message") or "ClamAV did not become ready.")
        time.sleep(3)


def run_acceptance(host: str, port: int, wait_seconds: int) -> dict:
    probe = _wait_for_clamd(host, port, wait_seconds)

    with tempfile.TemporaryDirectory(prefix="bookguard-clamav-acceptance-") as raw:
        root = Path(raw)
        clean_path = root / "clean.txt"
        eicar_path = root / "eicar.txt"
        clean_path.write_text(
            "BookGuard ClamAV acceptance test: harmless clean content.\n",
            encoding="utf-8",
        )
        eicar_path.write_bytes(_eicar_bytes())

        clean = scan_with_clamd(
            clean_path,
            host=host,
            port=port,
            timeout_seconds=30,
            max_bytes=1024 * 1024,
        )
        if not clean.get("clean") or clean.get("infected"):
            raise RuntimeError(f"ClamAV did not report the clean control as clean: {clean}")

        eicar = scan_with_clamd(
            eicar_path,
            host=host,
            port=port,
            timeout_seconds=30,
            max_bytes=1024 * 1024,
        )
        if not eicar.get("infected"):
            raise RuntimeError(f"ClamAV did not detect the EICAR test pattern: {eicar}")

        integrated = inspect_ebook_security(
            eicar_path,
            check_file_signatures=False,
            check_archive_safety=False,
            check_epub_structure=False,
            check_pdf_integrity=False,
            check_malware=True,
            clamd_host=host,
            clamd_port=port,
            malware_timeout_seconds=30,
            malware_max_bytes=1024 * 1024,
        )
        malware = integrated["checks"]["malwareScan"]
        if integrated.get("safe") or malware.get("status") != "failed":
            raise RuntimeError(
                "BookGuard did not fail closed on the EICAR malware result: "
                f"{integrated}"
            )

    return {
        "ok": True,
        "scanner": {
            "host": host,
            "port": port,
            "version": probe.get("version"),
        },
        "cleanControl": "clean",
        "eicarDetected": True,
        "signature": eicar.get("signature"),
        "bookguardFailClosed": True,
        "temporaryTestFilesRemoved": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run BookGuard's live ClamAV/EICAR acceptance test."
    )
    parser.add_argument(
        "--host",
        default=os.getenv("BOOKGUARD_CLAMD_HOST", "clamav"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("BOOKGUARD_CLAMD_PORT", "3310")),
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=180,
        help="How long to wait for initial ClamAV signature/database startup.",
    )
    args = parser.parse_args()

    try:
        result = run_acceptance(args.host, args.port, args.wait_seconds)
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "errorType": type(exc).__name__,
        }, indent=2))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

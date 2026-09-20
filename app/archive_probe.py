from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any
import zipfile


ZIP_CRC_TIMEOUT_SECONDS = 20
ZIP_CRC_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
ZIP_CRC_CPU_SECONDS = 15
MAX_WORKER_OUTPUT_BYTES = 64 * 1024


def inspect_zip_crc(path: str | Path) -> dict[str, Any]:
    """Run a full ZIP CRC check in a resource-limited child process."""
    target = Path(path)
    if not target.is_file():
        return {"error": "ZIP CRC probe target is not a regular file."}

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.archive_probe", "--crc-worker", str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=ZIP_CRC_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "ZIP CRC probe timed out."}
    except OSError as exc:
        return {"error": str(exc)[:500]}

    if proc.returncode != 0:
        return {"error": "ZIP CRC probe failed in the isolated worker."}
    if len(proc.stdout.encode("utf-8", errors="replace")) > MAX_WORKER_OUTPUT_BYTES:
        return {"error": "ZIP CRC probe returned too much output."}

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "ZIP CRC probe returned invalid JSON."}
    if not isinstance(payload, dict):
        return {"error": "ZIP CRC probe returned an invalid result."}
    return payload


def _apply_worker_limits() -> None:
    import resource

    resource.setrlimit(
        resource.RLIMIT_AS,
        (ZIP_CRC_ADDRESS_SPACE_BYTES, ZIP_CRC_ADDRESS_SPACE_BYTES),
    )
    resource.setrlimit(
        resource.RLIMIT_CPU,
        (ZIP_CRC_CPU_SECONDS, ZIP_CRC_CPU_SECONDS),
    )


def _crc_worker(path: str) -> int:
    try:
        _apply_worker_limits()
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
        payload = {"badMember": bad_member}
    except Exception as exc:
        payload = {"error": str(exc)[:500]}

    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--crc-worker":
        raise SystemExit(_crc_worker(sys.argv[2]))
    raise SystemExit(2)

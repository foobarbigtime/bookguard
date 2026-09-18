from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import zipfile


class SmokeTestFailure(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestFailure(message)


def _volume_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = payload.get("services")
    if not isinstance(services, dict):
        raise SmokeTestFailure("Compose output has no services object.")
    service = services.get("bookguard")
    if not isinstance(service, dict):
        raise SmokeTestFailure("Compose output has no bookguard service.")
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        raise SmokeTestFailure("BookGuard Compose output has no volume list.")

    mapped: dict[str, dict[str, Any]] = {}
    for volume in volumes:
        if not isinstance(volume, dict):
            raise SmokeTestFailure("Compose did not normalize a volume definition.")
        target = str(volume.get("target") or "")
        if not target:
            raise SmokeTestFailure("A Compose volume has no container target.")
        if target in mapped:
            raise SmokeTestFailure(f"Compose defines duplicate target {target}.")
        mapped[target] = volume
    return mapped


def validate_compose(payload: dict[str, Any], profile: str) -> dict[str, bool]:
    """Validate base and opt-in writable-alias storage topologies."""
    if profile not in {"base", "actions", "admission", "full"}:
        raise SmokeTestFailure(f"Unknown Compose profile: {profile}.")

    volumes = _volume_map(payload)
    services = payload.get("services") or {}
    service = services.get("bookguard") or {}
    security_opt = service.get("security_opt") or []
    checks = {
        "noNewPrivilegesEnabled": "no-new-privileges:true" in security_opt,
        "booksMountedReadOnly": bool(volumes.get("/books", {}).get("read_only")),
        "audiobooksMountedReadOnly": bool(
            volumes.get("/audiobooks", {}).get("read_only")
        ),
        "binderyDatabaseMountedReadOnly": bool(
            volumes.get("/bindery", {}).get("read_only")
        ),
        "stagingMountedWritable": (
            "/staging" in volumes and not bool(volumes["/staging"].get("read_only"))
        ),
        "quarantineMountedWritable": (
            "/quarantine" in volumes
            and not bool(volumes["/quarantine"].get("read_only"))
        ),
        "configMountedWritable": (
            "/config" in volumes and not bool(volumes["/config"].get("read_only"))
        ),
        "stagingSeparateFromLibrary": (
            volumes.get("/staging", {}).get("source")
            != volumes.get("/books", {}).get("source")
        ),
        "quarantineSeparateFromLibrary": (
            volumes.get("/quarantine", {}).get("source")
            != volumes.get("/books", {}).get("source")
        ),
    }

    action = volumes.get("/action-books")
    action_expected = profile in {"actions", "full"}
    if not action_expected:
        checks["writableActionAliasAbsent"] = action is None
    else:
        checks.update({
            "writableActionAliasPresent": action is not None,
            "writableActionAliasWritable": (
                action is not None and not bool(action.get("read_only"))
            ),
            "actionAliasMapsLibrary": (
                action is not None
                and action.get("source") == volumes.get("/books", {}).get("source")
            ),
        })

    admission = volumes.get("/admission-books")
    admission_expected = profile in {"admission", "full"}
    if not admission_expected:
        checks["writableAdmissionAliasAbsent"] = admission is None
    else:
        checks.update({
            "writableAdmissionAliasPresent": admission is not None,
            "writableAdmissionAliasWritable": (
                admission is not None and not bool(admission.get("read_only"))
            ),
            "admissionAliasMapsLibrary": (
                admission is not None
                and admission.get("source") == volumes.get("/books", {}).get("source")
            ),
        })

    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise SmokeTestFailure(
            f"Compose {profile} safety checks failed: {', '.join(failed)}"
        )
    return checks


class FakeBinderyClient:
    def __init__(self, stored_path: str) -> None:
        self.stored_path = stored_path
        self.import_mode = "auto"
        self.auto_grab = "false"
        self.registered = False
        self.queue: list[dict[str, Any]] = []
        self.scan_requests = 0
        self.removals: list[tuple[int, bool, bool]] = []
        self.candidate = {
            "guid": "bookguard-smoke-guid",
            "title": "Ann Patchett - Bel Canto retail epub",
            "nzbUrl": "https://invalid.example/bookguard-smoke.nzb",
            "size": 4096,
            "approved": True,
            "mediaType": "ebook",
            "protocol": "usenet",
            "indexerId": 1,
            "indexerName": "Isolated smoke test",
        }

    def get_setting(self, key: str) -> str:
        values = {
            "import.mode": self.import_mode,
            "import.drop_folder": "/isolated/bookguard-staging",
            "import.drop_layout": "flat",
            "import.drop_link_mode": "copy",
            "autoGrab.enabled": self.auto_grab,
        }
        return values[key]

    def list_queue(self) -> dict[str, Any]:
        return {"items": list(self.queue), "partial": False}

    def get_book(self, book_id: int) -> dict[str, Any]:
        return {
            "id": int(book_id),
            "title": "Bel Canto",
            "author": {"name": "Ann Patchett"},
            "ebookFilePath": self.stored_path if self.registered else "",
            "bookFiles": (
                [{"format": "ebook", "path": self.stored_path}]
                if self.registered
                else []
            ),
        }

    def search_book(self, book_id: int) -> dict[str, Any]:
        return {"results": [dict(self.candidate)]}

    def list_history(
        self,
        book_id: int,
        event_type: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return {"items": []}

    def grab(self, book_id: int, candidate: dict[str, Any]) -> dict[str, Any]:
        self.queue = [{
            "id": 77,
            "bookId": int(book_id),
            "title": candidate["title"],
            "status": "downloading",
            "protocol": candidate["protocol"],
        }]
        return {"queueItem": dict(self.queue[0])}

    def scan_library(self) -> dict[str, str]:
        self.scan_requests += 1
        return {"message": "isolated scan requested"}

    def remove_queue_item(
        self,
        queue_id: int,
        *,
        remove_from_client: bool = False,
        delete_files: bool = False,
    ) -> None:
        self.removals.append((queue_id, remove_from_client, delete_files))
        self.queue = [
            item for item in self.queue if int(item.get("id") or 0) != int(queue_id)
        ]


def _write_epub(path: Path) -> None:
    container = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    package = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Bel Canto</dc:title>
    <dc:creator>Ann Patchett</dc:creator>
    <dc:identifier>bookguard-isolated-smoke-test</dc:identifier>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>
"""
    body = "Bel Canto by Ann Patchett. " + ("An isolated fictional passage. " * 100)
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        f"<h1>Bel Canto</h1><p>by Ann Patchett</p><p>{body}</p>"
        "</body></html>"
    )
    with zipfile.ZipFile(path, "x") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/chapter.xhtml", chapter)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def run_workflow_smoke_test() -> dict[str, Any]:
    """Exercise the full workflow entirely inside a temporary filesystem."""
    from app import acquisition
    from app.admission import reconcile_admission
    from app.config import settings
    from app.db import (
        active_ebook_acquisitions,
        add_result,
        create_scan,
        finish_scan,
        init_local_db,
        latest_results,
    )

    with tempfile.TemporaryDirectory(prefix="bookguard-smoke-") as raw_root:
        root = Path(raw_root)
        config = root / "config"
        books = root / "books"
        staging = root / "staging"
        quarantine = root / "quarantine"
        audiobooks = root / "audiobooks"
        relative = Path("Ann Patchett/Bel Canto (2001)/Bel Canto - Ann Patchett.epub")
        for directory in (config, books, staging, quarantine, audiobooks):
            directory.mkdir()
        (books / relative.parent).mkdir(parents=True)

        settings.config_dir = str(config)
        settings.ebook_root = str(books)
        settings.ebook_bindery_prefix = "/data/media/books"
        settings.audiobook_root = str(audiobooks)
        settings.quarantine_root = str(quarantine)
        settings.verification_use_tika = False
        settings.allow_actions = False

        os.environ.update({
            "BOOKGUARD_STAGING_ROOT": str(staging),
            "BOOKGUARD_BINDERY_DROP_FOLDER": "/isolated/bookguard-staging",
            "BOOKGUARD_AUTOMATIC_REACQUISITION": "false",
            "BOOKGUARD_ADMISSION_ENABLED": "false",
            "BOOKGUARD_ADMISSION_ROOT": str(books),
            "BOOKGUARD_ADMISSION_BINDERY_ROOT": "/data/media/books",
            "BOOKGUARD_MAX_STAGED_EBOOK_BYTES": str(8 * 1024 * 1024),
        })

        init_local_db()
        create_scan("isolated-smoke-scan", 1)
        add_result("isolated-smoke-scan", {
            "file_id": 1,
            "book_id": 42,
            "author": "Ann Patchett",
            "title": "Bel Canto",
            "format": "ebook",
            "stored_path": str(Path("/data/media/books") / relative),
            "local_path": str(books / relative),
            "classification": "REVIEW",
            "risk_score": 80,
            "reason_code": "MISMATCH",
            "reasons": ["isolated smoke-test record"],
            "metadata": {},
        })
        finish_scan("isolated-smoke-scan")
        result = latest_results(limit=1)[0]
        client = FakeBinderyClient(result["stored_path"])

        disabled = acquisition.acquisition_readiness(client)
        _require(not disabled["ready"], "Default acquisition gate unexpectedly opened.")
        _require(
            {"automaticReacquisitionEnabled", "binderyExternalImport", "actionsEnabled"}
            <= set(disabled["blockers"]),
            "Default acquisition blockers were incomplete.",
        )

        settings.allow_actions = True
        client.import_mode = "external"
        os.environ["BOOKGUARD_AUTOMATIC_REACQUISITION"] = "true"
        os.environ["BOOKGUARD_ADMISSION_ENABLED"] = "true"

        started = acquisition.start_ebook_acquisition(
            result,
            client.candidate["guid"],
            client,
        )
        acquisition_id = int(started["acquisition"]["id"])
        _require(
            started["acquisition"]["status"] == "queued",
            "Acquisition was not durably queued.",
        )

        staged = staging / relative.name
        _write_epub(staged)
        expected_hash = _sha256(staged)

        downloading = acquisition.reconcile_ebook_acquisition(acquisition_id, client)
        _require(
            downloading["acquisition"]["status"] == "downloading"
            and downloading["acquisition"]["staged_sha256"] is None,
            "Staged bytes were accepted before Bindery completed its handoff.",
        )

        client.queue[0]["status"] = "importExternal"
        observed = acquisition.reconcile_ebook_acquisition(acquisition_id, client)
        _require(
            observed["acquisition"]["status"] == "staging_observed",
            "Stable-file observation was skipped.",
        )
        verified = acquisition.reconcile_ebook_acquisition(acquisition_id, client)
        _require(
            verified["acquisition"]["status"] == "verified"
            and verified["acquisition"]["staged_sha256"] == expected_hash,
            "Staged-byte verification did not reach the verified state.",
        )

        admitted = acquisition.admit_ebook_acquisition(acquisition_id, client)
        admission_id = int(admitted["admission"]["admissionId"])
        library = books / relative
        _require(
            library.is_file()
            and not library.is_symlink()
            and _sha256(library) == expected_hash,
            "Admission did not publish an independent verified library copy.",
        )
        _require(
            library.stat().st_ino != staged.stat().st_ino,
            "The library copy is tied to the staged inode.",
        )

        client.registered = True
        registration = reconcile_admission(admission_id, client)
        _require(registration["registered"], "Bindery registration was not recorded.")

        finalized = acquisition.finalize_ebook_acquisition(acquisition_id, client)
        _require(
            finalized["acquisition"]["status"] == "finalized",
            "Acquisition finalization did not reach its terminal state.",
        )
        _require(not staged.exists(), "Finalization retained the verified staging copy.")
        _require(
            library.is_file() and _sha256(library) == expected_hash,
            "Finalization changed the admitted library file.",
        )
        _require(client.queue == [], "Finalization retained the terminal queue record.")
        _require(
            client.removals == [(77, False, False)],
            "Finalization requested unsafe download-client cleanup flags.",
        )
        _require(
            active_ebook_acquisitions() == [],
            "A finalized acquisition remained active.",
        )

        return {
            "ok": True,
            "isolated": True,
            "networkRequired": False,
            "phases": [
                "default gates",
                "explicit acquisition",
                "completed-handoff enforcement",
                "stable staged-byte verification",
                "atomic admission",
                "Bindery registration",
                "explicit safe finalization",
            ],
            "sha256": expected_hash,
            "queueRemoval": {
                "removeFromClient": False,
                "deleteFiles": False,
            },
            "temporaryWorkspaceRemovedOnExit": True,
        }


def _read_json_stdin() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise SmokeTestFailure(f"Unable to read Compose JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SmokeTestFailure("Compose JSON must be an object.")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated BookGuard smoke tests.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    compose_parser = subparsers.add_parser("compose")
    compose_parser.add_argument(
        "profile",
        choices=("base", "actions", "admission", "full"),
    )
    subparsers.add_parser("workflow")
    args = parser.parse_args(argv)

    try:
        if args.command == "compose":
            result = {
                "ok": True,
                "profile": args.profile,
                "checks": validate_compose(_read_json_stdin(), args.profile),
            }
        else:
            result = run_workflow_smoke_test()
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

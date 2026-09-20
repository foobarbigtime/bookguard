from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Any

from app import __version__


MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "bookguard.db"
MANIFEST_SCHEMA_VERSION = 1
REQUIRED_TABLES = {
    "app_settings",
    "cleanup_actions",
    "ebook_acquisitions",
    "ebook_admissions",
    "metadata_repairs",
    "scan_results",
    "scans",
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class BackupError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"bookguard-{stamp}"


def _require_safe_name(name: str) -> str:
    value = str(name or "").strip()
    if not value or value in {".", ".."} or not _SAFE_NAME.fullmatch(value):
        raise BackupError(
            "Backup name may contain only letters, numbers, dot, underscore, and dash."
        )
    return value


def _require_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError(f"{label} is unavailable: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise BackupError(f"{label} must not be a symlink.")
    if not stat.S_ISREG(info.st_mode):
        raise BackupError(f"{label} must be a regular file.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_uri(path: Path, *, immutable: bool = False) -> str:
    suffix = "?mode=ro"
    if immutable:
        suffix += "&immutable=1"
    return path.resolve().as_uri() + suffix


def _inspect_database(path: Path, *, immutable: bool = False) -> dict[str, Any]:
    _require_regular_file(path, "BookGuard database")
    try:
        with sqlite3.connect(
            _readonly_uri(path, immutable=immutable),
            uri=True,
        ) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise BackupError(
                    f"SQLite integrity_check failed: {integrity[0] if integrity else 'no result'}"
                )
            tables = sorted(
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                ).fetchall()
            )
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise BackupError(f"Unable to validate BookGuard SQLite database: {exc}") from exc

    missing = sorted(REQUIRED_TABLES - set(tables))
    if missing:
        raise BackupError(
            "Backup database is missing required BookGuard tables: "
            + ", ".join(missing)
        )

    return {
        "integrity": "ok",
        "tables": tables,
        "userVersion": user_version,
    }


def create_backup(
    config_dir: str | Path,
    backup_root: str | Path,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    config = Path(config_dir).resolve()
    source = config / DATABASE_NAME
    _require_regular_file(source, "BookGuard source database")

    root = Path(backup_root).resolve()
    if root == config:
        raise BackupError("Backup root must be separate from BookGuard's config directory.")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise BackupError("Backup root is not a directory.")

    bundle_name = _require_safe_name(name or _default_name())
    bundle = root / bundle_name
    try:
        bundle.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise BackupError(f"Backup bundle already exists: {bundle}") from exc
    except OSError as exc:
        raise BackupError(f"Unable to create backup bundle: {exc}") from exc

    temp_db = bundle / f".{DATABASE_NAME}.tmp"
    final_db = bundle / DATABASE_NAME
    manifest_path = bundle / MANIFEST_NAME

    try:
        try:
            with sqlite3.connect(_readonly_uri(source), uri=True) as src:
                with sqlite3.connect(temp_db) as dst:
                    src.backup(dst)
                    dst.commit()
        except sqlite3.Error as exc:
            raise BackupError(f"SQLite online backup failed: {exc}") from exc

        _require_regular_file(temp_db, "Temporary backup database")
        db_report = _inspect_database(temp_db)
        with temp_db.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_db, final_db)

        manifest = {
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "createdAt": _utc_now(),
            "bookguardVersion": __version__,
            "database": {
                "file": DATABASE_NAME,
                "sha256": _sha256(final_db),
                "bytes": final_db.stat().st_size,
                "integrity": db_report["integrity"],
                "userVersion": db_report["userVersion"],
                "tables": db_report["tables"],
            },
        }
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        return {
            "ok": True,
            "bundle": str(bundle),
            "manifest": manifest,
        }
    except Exception:
        temp_db.unlink(missing_ok=True)
        if not manifest_path.exists() and not final_db.exists():
            try:
                bundle.rmdir()
            except OSError:
                pass
        raise


def validate_backup(bundle_dir: str | Path) -> dict[str, Any]:
    bundle = Path(bundle_dir)
    try:
        bundle_info = bundle.lstat()
    except OSError as exc:
        raise BackupError(f"Backup bundle is unavailable: {exc}") from exc
    if stat.S_ISLNK(bundle_info.st_mode):
        raise BackupError("Backup bundle must not be a symlink.")
    if not stat.S_ISDIR(bundle_info.st_mode):
        raise BackupError("Backup bundle must be a directory.")

    manifest_path = bundle / MANIFEST_NAME
    database_path = bundle / DATABASE_NAME
    _require_regular_file(manifest_path, "Backup manifest")
    _require_regular_file(database_path, "Backup database")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"Backup manifest is invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BackupError("Backup manifest must be a JSON object.")
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        raise BackupError("Backup manifest schema version is unsupported.")

    database_manifest = manifest.get("database")
    if not isinstance(database_manifest, dict):
        raise BackupError("Backup manifest has no database object.")
    if database_manifest.get("file") != DATABASE_NAME:
        raise BackupError("Backup manifest references an unexpected database filename.")

    expected_size = database_manifest.get("bytes")
    actual_size = database_path.stat().st_size
    if expected_size != actual_size:
        raise BackupError(
            f"Backup database size mismatch: expected {expected_size}, found {actual_size}."
        )

    expected_hash = str(database_manifest.get("sha256") or "")
    actual_hash = _sha256(database_path)
    if not expected_hash or expected_hash != actual_hash:
        raise BackupError("Backup database SHA-256 does not match the manifest.")

    db_report = _inspect_database(database_path, immutable=True)
    manifest_tables = database_manifest.get("tables")
    if manifest_tables != db_report["tables"]:
        raise BackupError("Backup database table inventory does not match the manifest.")

    return {
        "ok": True,
        "bundle": str(bundle.resolve()),
        "bookguardVersion": manifest.get("bookguardVersion"),
        "createdAt": manifest.get("createdAt"),
        "database": {
            "sha256": actual_hash,
            "bytes": actual_size,
            "integrity": db_report["integrity"],
            "userVersion": db_report["userVersion"],
            "tables": db_report["tables"],
        },
        "readOnlyValidation": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create or validate BookGuard database backup bundles."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser(
        "create",
        help="Create a transactionally consistent SQLite backup bundle.",
    )
    create_parser.add_argument("--config-dir", default=os.getenv("CONFIG_DIR", "/config"))
    create_parser.add_argument("--backup-root", required=True)
    create_parser.add_argument("--name")

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate an existing backup without modifying it.",
    )
    validate_parser.add_argument("bundle")

    args = parser.parse_args(argv)
    try:
        result = (
            create_backup(args.config_dir, args.backup_root, name=args.name)
            if args.command == "create"
            else validate_backup(args.bundle)
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "errorType": type(exc).__name__,
                },
                indent=2,
            )
        )
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

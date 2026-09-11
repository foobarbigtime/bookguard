from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .bindery_client import BinderyClient, BinderyClientError
from .config import settings


class PreImportSafetyError(RuntimeError):
    pass


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _setting_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        if "value" in value:
            return str(value.get("value") or "").strip()
        return ""
    return str(value).strip()


def preimport_readiness(client: BinderyClient | None = None) -> dict[str, Any]:
    """Report whether automatic reacquisition can be made content-safe.

    BookGuard only permits automatic grabbing when Bindery is configured for
    external import handoff. In that topology, completed downloads are copied
    or hardlinked to a staging/drop folder and Bindery waits instead of placing
    them directly into the managed library. BookGuard can then inspect the
    actual downloaded bytes before anything is admitted to the library.

    This function is read-only and intentionally fails closed.
    """
    client = client or BinderyClient()
    staging_root = Path(os.getenv("BOOKGUARD_STAGING_ROOT", "/staging")).resolve()
    expected_drop = os.getenv("BOOKGUARD_BINDERY_DROP_FOLDER", "").strip()
    enabled = _bool_env("BOOKGUARD_AUTOMATIC_REACQUISITION", False)

    try:
        import_mode = _setting_text(client.get_setting("import.mode")).lower()
        drop_folder = _setting_text(client.get_setting("import.drop_folder"))
        drop_layout = _setting_text(client.get_setting("import.drop_layout")).lower() or "flat"
        link_mode = _setting_text(client.get_setting("import.drop_link_mode")).lower() or "copy"
    except BinderyClientError as exc:
        raise PreImportSafetyError(str(exc)) from exc

    media_roots = [Path(settings.ebook_root).resolve(), Path(settings.audiobook_root).resolve()]
    quarantine_root = Path(settings.quarantine_root).resolve()
    outside_library = all(
        staging_root != root and root not in staging_root.parents
        for root in media_roots
    )
    separate_from_quarantine = staging_root != quarantine_root and quarantine_root not in staging_root.parents
    exists = staging_root.is_dir()
    writable = exists and os.access(staging_root, os.W_OK | os.X_OK)
    drop_matches = bool(expected_drop and drop_folder and expected_drop == drop_folder)

    checks = {
        "automaticReacquisitionEnabled": enabled,
        "binderyExternalImport": import_mode == "external",
        "binderyDropFolderConfigured": bool(drop_folder),
        "dropFolderMappingConfirmed": drop_matches,
        "stagingRootExists": exists,
        "stagingRootWritable": writable,
        "stagingOutsideLibraryRoots": outside_library,
        "stagingSeparateFromQuarantine": separate_from_quarantine,
        "supportedDropLayout": drop_layout in {"flat", "templated"},
        "supportedDropLinkMode": link_mode in {"copy", "hardlink"},
    }

    ready = all(checks.values())
    blockers = [name for name, ok in checks.items() if not ok]

    return {
        "ready": ready,
        "checks": checks,
        "blockers": blockers,
        "stagingRoot": str(staging_root),
        "expectedBinderyDropFolder": expected_drop,
        "bindery": {
            "importMode": import_mode,
            "dropFolder": drop_folder,
            "dropLayout": drop_layout,
            "dropLinkMode": link_mode,
        },
        "automaticGrabAllowed": ready,
        "message": (
            "Pre-import staging is ready. Automatic grabs may proceed only through the external-import staging path."
            if ready
            else "Automatic grabbing is blocked until every pre-import staging safety check passes."
        ),
    }

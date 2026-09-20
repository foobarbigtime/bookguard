from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .bindery_client import BinderyClient, BinderyClientError
from .config import ConfigurationError, load_automation_settings, settings
from .file_safety import is_within


class PreImportSafetyError(RuntimeError):
    pass


def _setting_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        if "value" in value:
            return str(value.get("value") or "").strip()
        return ""
    return str(value).strip()


# Built-in defaults for known Bindery import settings.
# When a setting has never been explicitly stored, Bindery returns HTTP 404.
# For these known settings, we can safely assume the default.
_BUILTIN_DEFAULTS = {
    "import.mode": "auto",
    "import.drop_folder": "",
    "import.drop_layout": "flat",
    "import.drop_link_mode": "copy",
}


def _get_setting_with_defaults(client: BinderyClient, key: str) -> Any:
    """Get a Bindery setting, falling back to built-in defaults for known settings.

    Bindery returns HTTP 404 when a setting has never been explicitly stored.
    For known settings, we have safe defaults. For unknown settings, we raise.
    """
    try:
        return client.get_setting(key)
    except BinderyClientError as exc:
        error_text = str(exc)
        # Check if this is a "setting not found" error (HTTP 404)
        if "HTTP 404" in error_text and "setting not found" in error_text:
            if key in _BUILTIN_DEFAULTS:
                return _BUILTIN_DEFAULTS[key]
        # Re-raise if this is a different error or an unknown setting
        raise


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
    try:
        automation = load_automation_settings()
    except ConfigurationError as exc:
        raise PreImportSafetyError(str(exc)) from exc
    staging_root = Path(automation.staging_root).resolve()
    expected_drop = automation.bindery_drop_folder
    enabled = automation.automatic_reacquisition

    try:
        import_mode = _setting_text(_get_setting_with_defaults(client, "import.mode")).lower()
        drop_folder = _setting_text(_get_setting_with_defaults(client, "import.drop_folder"))
        drop_layout = _setting_text(_get_setting_with_defaults(client, "import.drop_layout")).lower() or "flat"
        link_mode = _setting_text(_get_setting_with_defaults(client, "import.drop_link_mode")).lower() or "copy"
    except BinderyClientError as exc:
        raise PreImportSafetyError(str(exc)) from exc

    media_roots = [Path(settings.ebook_root).resolve(), Path(settings.audiobook_root).resolve()]
    quarantine_root = Path(settings.quarantine_root).resolve()
    outside_library = all(not is_within(staging_root, root) for root in media_roots)
    separate_from_quarantine = not (
        is_within(staging_root, quarantine_root)
        or is_within(quarantine_root, staging_root)
    )
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

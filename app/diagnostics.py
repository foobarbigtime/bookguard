from __future__ import annotations

import os
from pathlib import Path
import platform
from typing import Any, Callable

from . import __version__
from .config import ConfigurationError, load_automation_settings, settings
from .malware_scan import probe_clamd
from .operator_guidance import explain_blockers, humanize_key
from .preimport import preimport_readiness


_GATE_LABELS = {
    "actionsEnabled": "Bindery actions",
    "ebookActionsEnabled": "Ebook file actions",
    "malwareScanningEnabled": "Malware scanning",
    "malwareScannerConfigured": "Malware scanner configured",
}


def _gate_cards(gates: dict[str, bool]) -> list[dict[str, object]]:
    return [
        {
            "key": key,
            "label": _GATE_LABELS.get(key, humanize_key(key)),
            "enabled": bool(enabled),
        }
        for key, enabled in gates.items()
    ]


def _mount_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _mount_info(path: str) -> dict[str, Any]:
    target = str(Path(path).resolve(strict=False))
    best: dict[str, Any] | None = None
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            for raw_line in handle:
                before, separator, _ = raw_line.partition(" - ")
                if not separator:
                    continue
                fields = before.split()
                if len(fields) < 6:
                    continue
                mount_point = _mount_field(fields[4])
                prefix = mount_point.rstrip("/") + "/"
                if target != mount_point and not target.startswith(prefix):
                    continue
                options = set(fields[5].split(","))
                candidate = {
                    "mountPoint": mount_point,
                    "readOnly": "ro" in options and "rw" not in options,
                }
                if best is None or len(mount_point) > len(str(best["mountPoint"])):
                    best = candidate
    except OSError:
        return {"mountPoint": None, "readOnly": None}
    return best or {"mountPoint": None, "readOnly": None}


def _path_status(
    key: str,
    label: str,
    path: str,
    *,
    expected_writable: bool,
) -> dict[str, Any]:
    normalized = str(path or "").strip()
    exists = bool(normalized) and os.path.exists(normalized)
    mount = _mount_info(normalized) if normalized else {"mountPoint": None, "readOnly": None}
    read_only = mount.get("readOnly")
    if not exists:
        observed = "missing"
    elif read_only is True:
        observed = "read-only"
    elif read_only is False:
        observed = "writable mount"
    else:
        observed = "unknown"

    expectation_met = bool(exists) and (
        (read_only is False) if expected_writable else (read_only is True)
    )
    return {
        "key": key,
        "label": label,
        "path": normalized,
        "exists": exists,
        "expectedWritable": expected_writable,
        "expectedMode": "writable" if expected_writable else "read-only",
        "observedMode": observed,
        "mountPoint": mount.get("mountPoint"),
        "expectationMet": expectation_met,
    }


def _build_report() -> dict[str, Any]:
    image_version = os.getenv("BOOKGUARD_BUILD_VERSION", "").strip() or "unknown"
    revision = os.getenv("BOOKGUARD_BUILD_REVISION", "").strip() or "unknown"
    source = os.getenv("BOOKGUARD_BUILD_SOURCE", "").strip() or "unknown"
    complete = (
        image_version == __version__
        and revision != "unknown"
        and source != "unknown"
    )
    return {
        "applicationVersion": __version__,
        "imageVersion": image_version,
        "revision": revision,
        "source": source,
        "provenanceComplete": complete,
    }


def _malware_report() -> dict[str, Any]:
    configured = bool(settings.verification_clamd_host)
    if not configured:
        return {
            "enabled": bool(settings.verification_malware_scan),
            "configured": False,
            "reachable": False,
            "status": "not_configured",
            "message": "No ClamAV endpoint is configured.",
            "version": None,
        }

    probe = probe_clamd(
        host=settings.verification_clamd_host,
        port=settings.verification_clamd_port,
        timeout_seconds=max(
            1,
            min(int(settings.verification_malware_timeout_seconds), 3),
        ),
    )
    return {
        "enabled": bool(settings.verification_malware_scan),
        "configured": True,
        "reachable": bool(probe.get("ok")),
        "status": "available" if probe.get("ok") else "unavailable",
        "message": str(probe.get("message") or ""),
        "version": probe.get("version"),
    }


def _deployment_report(automation: Any | None) -> dict[str, Any]:
    mounts = [
        _path_status("application", "Application filesystem", "/app", expected_writable=False),
        _path_status("temporary", "Temporary workspace", "/tmp", expected_writable=True),
        _path_status("config", "BookGuard config", settings.config_dir, expected_writable=True),
        _path_status("binderyDb", "Bindery database", settings.bindery_db, expected_writable=False),
        _path_status("ebooks", "Ebook library", settings.ebook_root, expected_writable=False),
        _path_status("audiobooks", "Audiobook library", settings.audiobook_root, expected_writable=False),
        _path_status("quarantine", "Quarantine", settings.quarantine_root, expected_writable=True),
    ]
    if automation is not None:
        mounts.append(
            _path_status(
                "staging",
                "Pre-import staging",
                automation.staging_root,
                expected_writable=True,
            )
        )
        if automation.ebook_actions_enabled or os.path.exists(automation.ebook_action_root):
            mounts.append(
                _path_status(
                    "ebookActions",
                    "Writable ebook action alias",
                    automation.ebook_action_root,
                    expected_writable=True,
                )
            )

    return {
        "runtime": {
            "uid": os.getuid(),
            "gid": os.getgid(),
            "pythonVersion": platform.python_version(),
            "nonRoot": os.getuid() != 0,
        },
        "build": _build_report(),
        "malware": _malware_report(),
        "mounts": mounts,
    }


def _readiness_section(
    key: str,
    label: str,
    loader: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    try:
        raw = loader()
    except Exception as exc:
        return {
            "key": key,
            "label": label,
            "ready": False,
            "state": "error",
            "message": str(exc)[:1000],
            "blockers": [],
            "explanations": [],
            "details": {},
        }

    blockers = [str(item) for item in raw.get("blockers") or []]
    return {
        "key": key,
        "label": label,
        "ready": bool(raw.get("ready")),
        "state": "ready" if raw.get("ready") else "blocked",
        "message": str(raw.get("message") or ""),
        "blockers": blockers,
        "explanations": explain_blockers(blockers),
        "details": {
            name: value
            for name, value in raw.items()
            if name not in {"checks", "blockers", "message", "ready"}
        },
    }


def diagnostics_snapshot() -> dict[str, Any]:
    """Build a read-only, secret-free explanation of current safety gates."""
    sections = [
        _readiness_section("preimport", "Pre-import staging", preimport_readiness),
    ]

    automation = None
    automation_mode = "invalid"
    try:
        automation = load_automation_settings()
        automation_mode = automation.automation_mode
        deployment_error = None
        gates = {
            "actionsEnabled": bool(settings.allow_actions),
            "ebookActionsEnabled": bool(automation.ebook_actions_enabled),
            "malwareScanningEnabled": bool(settings.verification_malware_scan),
            "malwareScannerConfigured": bool(settings.verification_clamd_host),
        }
    except ConfigurationError as exc:
        deployment_error = str(exc)
        gates = {
            "actionsEnabled": bool(settings.allow_actions),
            "malwareScanningEnabled": bool(settings.verification_malware_scan),
            "malwareScannerConfigured": bool(settings.verification_clamd_host),
        }

    return {
        "ok": deployment_error is None,
        "deploymentError": deployment_error,
        "automationMode": automation_mode,
        "gates": gates,
        "gateCards": _gate_cards(gates),
        "sections": sections,
        "deployment": _deployment_report(automation),
        "secretsIncluded": False,
    }

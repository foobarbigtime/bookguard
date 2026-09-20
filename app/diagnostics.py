from __future__ import annotations

from typing import Any, Callable

from .acquisition import acquisition_readiness
from .acquisition_coordinator import acquisition_coordinator_status
from .admission import admission_readiness
from .config import ConfigurationError, load_automation_settings, settings
from .preimport import preimport_readiness


_GATE_LABELS = {
    "actionsEnabled": "Bindery actions",
    "automaticReacquisitionEnabled": "Automatic reacquisition",
    "coordinatorEnabled": "Supervised coordinator",
    "admissionEnabled": "Direct admission",
    "ebookActionsEnabled": "Ebook file actions",
    "malwareScanningEnabled": "Malware scanning",
    "malwareScannerConfigured": "Malware scanner configured",
}


_BLOCKER_HELP: dict[str, tuple[str, str, str]] = {
    "automaticReacquisitionEnabled": (
        "Automatic reacquisition is disabled",
        "BookGuard will not start or advance the guarded reacquisition workflow while this deployment gate is off.",
        "Set BOOKGUARD_AUTOMATIC_REACQUISITION=true only when you intentionally want the supervised reacquisition workflow available.",
    ),
    "binderyExternalImport": (
        "Bindery is not using external import mode",
        "Completed downloads must be handed to BookGuard staging before Bindery places them in the library.",
        "Set Bindery import.mode to external before using the guarded acquisition/admission workflow.",
    ),
    "binderyDropFolderConfigured": (
        "Bindery drop folder is not configured",
        "BookGuard cannot prove where Bindery will hand completed downloads to the staging workflow.",
        "Configure Bindery import.drop_folder to the BookGuard staging handoff path.",
    ),
    "dropFolderMappingConfirmed": (
        "Bindery drop-folder mapping does not match",
        "The Bindery-visible drop folder does not match BOOKGUARD_BINDERY_DROP_FOLDER, so BookGuard cannot prove both services refer to the same handoff location.",
        "Make Bindery import.drop_folder and BOOKGUARD_BINDERY_DROP_FOLDER describe the same shared staging folder.",
    ),
    "stagingRootExists": (
        "BookGuard staging root is missing",
        "The configured staging directory does not currently exist inside the BookGuard container.",
        "Create or mount BOOKGUARD_STAGING_ROOT at the expected container path.",
    ),
    "stagingRootWritable": (
        "BookGuard staging root is not writable",
        "The guarded workflow needs its staging mount to be writable for verified handoff and cleanup.",
        "Fix the staging bind mount or filesystem permissions without making the library mounts writable.",
    ),
    "stagingOutsideLibraryRoots": (
        "Staging overlaps a media library",
        "Pre-import staging must remain outside both ebook and audiobook library roots.",
        "Move BOOKGUARD_STAGING_ROOT to a separate directory outside the managed media trees.",
    ),
    "stagingSeparateFromQuarantine": (
        "Staging overlaps quarantine",
        "Staging and quarantine must be separate trees so safety actions cannot confuse handoff bytes with quarantined media.",
        "Use distinct non-overlapping staging and quarantine paths.",
    ),
    "supportedDropLayout": (
        "Bindery drop layout is unsupported",
        "BookGuard only understands the Bindery external-import layouts it can deterministically map.",
        "Use Bindery import.drop_layout flat or templated.",
    ),
    "supportedDropLinkMode": (
        "Bindery drop link mode is unsupported",
        "The external handoff must use a copy or hard link mode that BookGuard can validate safely.",
        "Use Bindery import.drop_link_mode copy or hardlink.",
    ),
    "actionsEnabled": (
        "BookGuard actions are disabled",
        "Mutating recovery/admission operations remain blocked while BOOKGUARD_ALLOW_ACTIONS is off.",
        "Enable BOOKGUARD_ALLOW_ACTIONS only for a deliberate guarded action session.",
    ),
    "binderyAutoGrabDisabled": (
        "Bindery auto-grab is still enabled",
        "A controlled acquisition requires BookGuard to be the only component initiating the selected replacement grab.",
        "Disable Bindery autoGrab.enabled before starting a controlled acquisition.",
    ),
    "binderyQueueComplete": (
        "Bindery queue response is partial",
        "BookGuard cannot prove the absence of conflicting queue work from a partial queue snapshot.",
        "Resolve the condition causing Bindery to return a partial queue before continuing.",
    ),
    "binderyQueueIdle": (
        "Bindery queue is not idle",
        "Active or unknown queue entries can race with a guarded replacement workflow.",
        "Wait for or resolve active Bindery queue work before starting the acquisition.",
    ),
    "stagingInventoryComplete": (
        "Staging inventory was truncated",
        "BookGuard cannot prove the staging folder is empty when its inventory result is incomplete.",
        "Reduce or clear staging contents so BookGuard can inspect the complete staging inventory.",
    ),
    "stagingEmpty": (
        "Staging already contains files",
        "A new controlled acquisition starts only from an empty staging area to avoid ambiguous byte ownership.",
        "Review and safely clear or relocate existing staging files before starting another acquisition.",
    ),
    "noActiveAcquisition": (
        "Another BookGuard acquisition is active",
        "BookGuard permits only one durable supervised acquisition at a time.",
        "Finish or explicitly recover the existing acquisition before starting another.",
    ),
    "admissionEnabled": (
        "Direct admission is disabled",
        "Publishing verified staged bytes into the library is separately gated from acquisition.",
        "Set BOOKGUARD_ADMISSION_ENABLED=true only when the admission overlay and all other admission gates are intentionally ready.",
    ),
    "admissionRootExists": (
        "Writable admission root is missing",
        "BookGuard cannot publish through a writable alias that does not exist inside the container.",
        "Start BookGuard with the admission overlay and confirm BOOKGUARD_ADMISSION_ROOT is mounted.",
    ),
    "admissionRootWritable": (
        "Writable admission root is not writable",
        "Direct admission requires its narrow writable alias while the normal library mount remains read-only.",
        "Fix the admission alias mount or permissions; do not make the normal /books mount writable.",
    ),
    "binderyRootMappingConfirmed": (
        "Admission library mapping does not match",
        "BookGuard cannot prove the writable admission alias maps to the same Bindery ebook library root.",
        "Set BOOKGUARD_ADMISSION_BINDERY_ROOT to the exact Bindery ebook library prefix.",
    ),
    "stagingOutsideAdmissionRoot": (
        "Staging overlaps the writable admission root",
        "Source staging and destination admission trees must be distinct for safe no-overwrite publication.",
        "Use separate non-overlapping staging and admission roots.",
    ),
    "quarantineOutsideAdmissionRoot": (
        "Quarantine overlaps the writable admission root",
        "Quarantine must not share a tree with the narrow writable admission alias.",
        "Use separate non-overlapping quarantine and admission roots.",
    ),
    "coordinatorEnabled": (
        "Supervised coordinator is disabled",
        "The background coordinator will not resume operator-started durable acquisition state while its deployment gate is off.",
        "Set BOOKGUARD_ACQUISITION_COORDINATOR_ENABLED=true only if you want guarded state advancement between operator decisions.",
    ),
    "validConfiguration": (
        "Automation configuration is invalid",
        "One or more deployment-only automation values could not be parsed safely.",
        "Review the BookGuard deployment environment and correct the invalid value before enabling automation.",
    ),
    "singleActiveAcquisition": (
        "Multiple active acquisitions were found",
        "The coordinator refuses to choose between multiple durable acquisition sessions.",
        "Inspect the Attention page and explicitly resolve the extra active acquisition state.",
    ),
}


def _humanize(key: str) -> str:
    output = []
    for char in str(key):
        if char.isupper() and output:
            output.append(" ")
        output.append(char)
    return "".join(output).replace("_", " ").strip().capitalize()


def explain_blockers(blockers: list[str] | tuple[str, ...] | None) -> list[dict[str, str]]:
    explained = []
    for key in blockers or []:
        label, why, fix = _BLOCKER_HELP.get(
            str(key),
            (
                _humanize(str(key)),
                "This safety check did not pass.",
                "Review the underlying readiness details before continuing.",
            ),
        )
        explained.append({"key": str(key), "label": label, "why": why, "fix": fix})
    return explained


def _gate_cards(gates: dict[str, bool]) -> list[dict[str, object]]:
    return [
        {
            "key": key,
            "label": _GATE_LABELS.get(key, _humanize(key)),
            "enabled": bool(enabled),
        }
        for key, enabled in gates.items()
    ]


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
        _readiness_section("acquisition", "Controlled acquisition", acquisition_readiness),
        _readiness_section("admission", "Direct admission", admission_readiness),
    ]

    coordinator = acquisition_coordinator_status()
    coordinator_blockers = [str(item) for item in coordinator.get("blockers") or []]
    coordinator_state = str(coordinator.get("state") or "unknown")
    sections.append(
        {
            "key": "coordinator",
            "label": "Supervised coordinator",
            "ready": not coordinator_blockers and coordinator_state not in {"error", "blocked"},
            "state": coordinator_state,
            "message": str(coordinator.get("lastError") or ""),
            "blockers": coordinator_blockers,
            "explanations": explain_blockers(coordinator_blockers),
            "details": {
                "enabled": bool(coordinator.get("enabled")),
                "running": bool(coordinator.get("running")),
                "action": coordinator.get("action"),
                "lastRunAt": coordinator.get("lastRunAt"),
                "lastSuccessAt": coordinator.get("lastSuccessAt"),
            },
        }
    )

    try:
        automation = load_automation_settings()
        deployment_error = None
        gates = {
            "actionsEnabled": bool(settings.allow_actions),
            "automaticReacquisitionEnabled": bool(automation.automatic_reacquisition),
            "coordinatorEnabled": bool(automation.acquisition_coordinator_enabled),
            "admissionEnabled": bool(automation.admission_enabled),
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
        "gates": gates,
        "gateCards": _gate_cards(gates),
        "sections": sections,
        "secretsIncluded": False,
    }

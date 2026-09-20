from __future__ import annotations

_BLOCKER_HELP: dict[str, tuple[str, str, str]] = {
    "observeModeActive": (
        "Observe Mode disables the supervised coordinator",
        "Observe Mode is intentionally non-mutating, so BookGuard will not run the background acquisition coordinator even if its older deployment gates are enabled.",
        "Keep Observe Mode enabled while validating decisions. Return BOOKGUARD_AUTOMATION_MODE to manual only when you intentionally want the supervised mutation workflow available again.",
    ),
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


def humanize_key(key: str) -> str:
    return _humanize(key)


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


# Blockers emitted by the supervised coordinator itself. Keeping these here makes
# Diagnostics, Attention, and future automatic-mode reporting use the same wording.
_BLOCKER_HELP.update({
    "explicitAdmissionRequired": (
        "Verified acquisition is waiting for admission",
        "The supervised coordinator intentionally stops after staged-byte verification because admission is still an explicit operator decision.",
        "Review the verified acquisition in the guarded workflow and approve admission only if the intended book and staged file are correct.",
    ),
    "registrationConflict": (
        "Bindery registered the admitted path to the wrong book",
        "BookGuard will not finalize while Bindery's registration disagrees with the intended durable acquisition.",
        "Open the guarded workflow, review the conflicting registration, and use the explicit correction path only after confirming the intended book and file.",
    ),
    "registrationCorrectionInterrupted": (
        "Registration correction was interrupted",
        "BookGuard cannot safely infer whether an interrupted registration correction completed.",
        "Review the durable admission and correction state, then explicitly resume or recover the guarded correction.",
    ),
    "safeTransitionAvailable": (
        "No safe automatic transition is available",
        "The coordinator reached durable state that it is not allowed to advance without additional evidence or operator intervention.",
        "Review the recorded error and operation detail. Resolve the stated condition before asking BookGuard to continue.",
    ),
    "coordinatorHealthy": (
        "Coordinator encountered an unexpected error",
        "The background coordinator stopped rather than guessing after an unexpected runtime failure.",
        "Review the recorded error and Diagnostics. Correct the underlying problem before restarting or resuming the workflow.",
    ),
})


_OPERATION_HELP: dict[tuple[str, str], tuple[str, str, str]] = {
    ("acquisition", "review_required"): (
        "Replacement needs review",
        "BookGuard could not prove the staged replacement is safe and correct strongly enough to continue automatically.",
        "Review the verification evidence and choose whether to reject the candidate or continue through the guarded workflow.",
    ),
    ("acquisition", "cleanup_required"): (
        "Final cleanup needs recovery",
        "The intended library state was retained, but BookGuard could not prove that temporary acquisition cleanup completed.",
        "Review the acquisition detail and safely complete or recover the remaining cleanup step. Do not delete library media as part of recovery.",
    ),
    ("acquisition", "finalizing"): (
        "Finalization was interrupted",
        "BookGuard recorded a finalization-in-progress state and will not assume an interrupted cleanup completed.",
        "Review the durable acquisition state and resume the guarded finalization path.",
    ),
    ("acquisition", "failed"): (
        "Acquisition failed",
        "The guarded replacement workflow stopped before it could complete safely.",
        "Read the recorded error and operation evidence, correct the underlying cause, then retry through the guarded workflow.",
    ),
    ("admission", "registration_conflict"): (
        "Bindery registration conflicts with the intended book",
        "The admitted file exists, but Bindery associated it with a different book than BookGuard expected.",
        "Review the registration conflict and use the explicit guarded correction path only after confirming the intended mapping.",
    ),
    ("admission", "registration_correcting"): (
        "Registration correction needs recovery",
        "A guarded Bindery registration correction was interrupted, so BookGuard will not assume its outcome.",
        "Inspect the durable correction state and explicitly resume or recover it before continuing.",
    ),
    ("admission", "failed"): (
        "Admission failed",
        "BookGuard stopped because it could not safely complete publication or registration.",
        "Review the recorded error and verify the staged and library paths before retrying the guarded admission.",
    ),
    ("hardlink_correction", "*"): (
        "Shared-file correction needs review",
        "A shared-file correction journal remains unresolved, so BookGuard will not perform another correction on top of uncertain state.",
        "Inspect the correction snapshot and recorded error, then explicitly recover or resolve the journal before another shared-file action.",
    ),
    ("hardlink_cleanup", "*"): (
        "Staging-link cleanup needs review",
        "BookGuard cannot prove that cleanup of a staging hard-link alias completed.",
        "Inspect the cleanup snapshot and confirm the retained library file before explicitly recovering the staging-link cleanup.",
    ),
    ("cleanup", "failed"): (
        "Cleanup failed",
        "BookGuard could not prove that the requested cleanup completed safely.",
        "Review the recorded error and confirm the retained library media before retrying any cleanup action.",
    ),
    ("repair", "failed"): (
        "Metadata repair failed",
        "The guarded metadata repair did not complete successfully.",
        "Review the recorded error and before/after evidence before deciding whether to retry or undo any related change.",
    ),
    ("verification", "insufficient_evidence"): (
        "Verification evidence is insufficient",
        "The file may be structurally safe, but BookGuard does not have enough identity evidence to prove it belongs to the expected book.",
        "Review the stored verification evidence. Leave the item for manual review unless stronger deterministic evidence becomes available.",
    ),
    ("verification", "wrong_content"): (
        "Verification found different content",
        "The extracted identity evidence points away from the expected book.",
        "Review the evidence and keep the file out of automatic admission unless the catalog identity is corrected and verification passes.",
    ),
    ("verification", "unsafe_file"): (
        "File failed a safety check",
        "A deterministic file-safety check did not pass.",
        "Keep the file out of the library and review the specific safety evidence before deciding on quarantine or replacement.",
    ),
}


def operation_guidance(
    kind: str,
    status: str,
    error: str | None = None,
) -> dict[str, str] | None:
    normalized_kind = str(kind or "").casefold().replace("-", "_")
    normalized_status = str(status or "").casefold()
    item = _OPERATION_HELP.get((normalized_kind, normalized_status))
    if (
        item is None
        and normalized_kind in {"hardlink_correction", "hardlink_cleanup"}
        and normalized_status not in {"applied", "cancelled", "complete", "completed"}
    ):
        item = _OPERATION_HELP.get((normalized_kind, "*"))
    if item is None:
        return None
    label, why, next_step = item
    return {
        "label": label,
        "why": why,
        "nextStep": next_step,
        "recordedError": str(error or "").strip(),
    }


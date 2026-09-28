from app.operator_guidance import explain_blockers, operation_guidance


def test_coordinator_blockers_have_actionable_explanations():
    items = explain_blockers(
        [
            "explicitAdmissionRequired",
            "registrationConflict",
            "registrationCorrectionInterrupted",
            "safeTransitionAvailable",
            "coordinatorHealthy",
        ]
    )

    assert [item["key"] for item in items] == [
        "explicitAdmissionRequired",
        "registrationConflict",
        "registrationCorrectionInterrupted",
        "safeTransitionAvailable",
        "coordinatorHealthy",
    ]
    assert all(item["why"] for item in items)
    assert all(item["fix"] for item in items)


def test_operation_guidance_explains_verification_review():
    guidance = operation_guidance(
        "verification",
        "insufficient_evidence",
        "Only weak title evidence was found.",
    )

    assert guidance is not None
    assert guidance["label"] == "Verification evidence is insufficient"
    assert "identity evidence" in guidance["why"]
    assert "manual review" in guidance["nextStep"]
    assert guidance["recordedError"] == "Only weak title evidence was found."


def test_resolved_hardlink_record_has_no_recovery_guidance():
    assert operation_guidance("hardlink-correction", "applied") is None
    assert operation_guidance("hardlink_cleanup", "cancelled") is None


def test_unresolved_hardlink_record_has_recovery_guidance():
    guidance = operation_guidance(
        "hardlink-correction",
        "needs_review",
        "Filesystem changed during correction.",
    )

    assert guidance is not None
    assert guidance["label"] == "Shared-file correction needs review"
    assert "explicitly recover" in guidance["nextStep"]

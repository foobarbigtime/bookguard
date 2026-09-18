import pytest

from tools.smoke_test import SmokeTestFailure, validate_compose


def _compose_payload(
    include_admission: bool,
    include_action: bool = False,
) -> dict:
    volumes = [
        {"source": "/host/config", "target": "/config", "read_only": False},
        {"source": "/host/bindery", "target": "/bindery", "read_only": True},
        {"source": "/host/audio", "target": "/audiobooks", "read_only": True},
        {"source": "/host/books", "target": "/books", "read_only": True},
        {"source": "/host/quarantine", "target": "/quarantine", "read_only": False},
        {"source": "/host/staging", "target": "/staging", "read_only": False},
    ]
    if include_admission:
        volumes.append({
            "source": "/host/books",
            "target": "/admission-books",
            "read_only": False,
        })
    if include_action:
        volumes.append({
            "source": "/host/books",
            "target": "/action-books",
            "read_only": False,
        })
    return {
        "services": {
            "bookguard": {
                "security_opt": ["no-new-privileges:true"],
                "cap_drop": ["ALL"],
                "cap_add": ["DAC_READ_SEARCH"],
                "volumes": volumes,
            }
        }
    }


def test_base_compose_profile_has_no_writable_admission_alias():
    checks = validate_compose(_compose_payload(False), "base")

    assert checks["booksMountedReadOnly"] is True
    assert checks["writableActionAliasAbsent"] is True
    assert checks["writableAdmissionAliasAbsent"] is True


def test_action_profile_keeps_books_read_only_and_maps_writable_alias():
    checks = validate_compose(_compose_payload(False, True), "actions")

    assert checks["booksMountedReadOnly"] is True
    assert checks["writableActionAliasWritable"] is True
    assert checks["actionAliasMapsLibrary"] is True
    assert checks["writableAdmissionAliasAbsent"] is True


def test_admission_profile_keeps_books_read_only_and_maps_writable_alias():
    checks = validate_compose(_compose_payload(True), "admission")

    assert checks["booksMountedReadOnly"] is True
    assert checks["writableAdmissionAliasWritable"] is True
    assert checks["admissionAliasMapsLibrary"] is True
    assert checks["writableActionAliasAbsent"] is True


def test_full_profile_maps_distinct_action_and_admission_aliases():
    checks = validate_compose(_compose_payload(True, True), "full")

    assert checks["actionAliasMapsLibrary"] is True
    assert checks["admissionAliasMapsLibrary"] is True


def test_compose_profile_requires_no_new_privileges():
    payload = _compose_payload(False)
    payload["services"]["bookguard"].pop("security_opt")

    with pytest.raises(SmokeTestFailure, match="noNewPrivilegesEnabled"):
        validate_compose(payload, "base")


def test_compose_profile_requires_all_capabilities_dropped():
    payload = _compose_payload(False)
    payload["services"]["bookguard"].pop("cap_drop")

    with pytest.raises(SmokeTestFailure, match="allCapabilitiesDropped"):
        validate_compose(payload, "base")


def test_compose_profile_requires_bindery_read_capability():
    payload = _compose_payload(False)
    payload["services"]["bookguard"].pop("cap_add")

    with pytest.raises(SmokeTestFailure, match="binderyReadCapabilityAdded"):
        validate_compose(payload, "base")


def test_compose_profile_rejects_dac_override_capability():
    payload = _compose_payload(False)
    payload["services"]["bookguard"]["cap_add"].append("DAC_OVERRIDE")

    with pytest.raises(SmokeTestFailure, match="writeBypassCapabilityAbsent"):
        validate_compose(payload, "base")


def test_compose_profile_rejects_writable_books_mount():
    payload = _compose_payload(False)
    payload["services"]["bookguard"]["volumes"][3]["read_only"] = False

    with pytest.raises(SmokeTestFailure, match="booksMountedReadOnly"):
        validate_compose(payload, "base")

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
                "read_only": True,
                "tmpfs": ["/tmp:rw,nosuid,nodev,noexec,size=67108864"],
                "security_opt": ["no-new-privileges:true"],
                "cap_drop": ["ALL"],
                "user": "99:100",
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


def test_removed_admission_profiles_are_rejected():
    for profile in ("admission", "full"):
        with pytest.raises(SmokeTestFailure):
            validate_compose(_compose_payload(False), profile)


def test_admission_alias_is_flagged_in_every_profile():
    with pytest.raises(SmokeTestFailure, match="writableAdmissionAliasAbsent"):
        validate_compose(_compose_payload(True), "base")


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


def test_compose_profile_requires_non_root_runtime_user():
    payload = _compose_payload(False)
    payload["services"]["bookguard"]["user"] = "0:0"

    with pytest.raises(SmokeTestFailure, match="runtimeUserNonRoot"):
        validate_compose(payload, "base")


def test_compose_profile_rejects_added_capabilities():
    payload = _compose_payload(False)
    payload["services"]["bookguard"]["cap_add"] = ["DAC_READ_SEARCH"]

    with pytest.raises(SmokeTestFailure, match="noCapabilitiesAdded"):
        validate_compose(payload, "base")


def test_compose_profile_rejects_dac_override_capability():
    payload = _compose_payload(False)
    payload["services"]["bookguard"]["cap_add"] = ["DAC_OVERRIDE"]

    with pytest.raises(SmokeTestFailure, match="noCapabilitiesAdded"):
        validate_compose(payload, "base")


def test_compose_profile_rejects_writable_books_mount():
    payload = _compose_payload(False)
    payload["services"]["bookguard"]["volumes"][3]["read_only"] = False

    with pytest.raises(SmokeTestFailure, match="booksMountedReadOnly"):
        validate_compose(payload, "base")



def test_compose_profile_requires_read_only_root_filesystem():
    payload = _compose_payload(False)
    payload["services"]["bookguard"]["read_only"] = False

    with pytest.raises(SmokeTestFailure, match="rootFilesystemReadOnly"):
        validate_compose(payload, "base")


def test_compose_profile_requires_hardened_tmpfs():
    payload = _compose_payload(False)
    payload["services"]["bookguard"]["tmpfs"] = ["/tmp:rw,size=67108864"]

    with pytest.raises(SmokeTestFailure, match="tmpfsNoExec"):
        validate_compose(payload, "base")

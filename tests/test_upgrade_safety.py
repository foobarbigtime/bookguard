from __future__ import annotations

import copy

import pytest

from tools.upgrade_safety import UpgradeSafetyError, validate_runtime


def _inspect_payload() -> list[dict]:
    return [{
        "Config": {"User": "99:100"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Privileged": False,
            "SecurityOpt": ["no-new-privileges:true"],
            "CapDrop": ["ALL"],
            "Tmpfs": {
                "/tmp": "rw,nosuid,nodev,noexec,size=64m",
            },
        },
        "Mounts": [
            {"Destination": "/config", "RW": True},
            {"Destination": "/staging", "RW": True},
            {"Destination": "/quarantine", "RW": True},
            {"Destination": "/bindery", "RW": False},
            {"Destination": "/books", "RW": False},
            {"Destination": "/audiobooks", "RW": False},
        ],
    }]


def test_runtime_hardening_accepts_expected_production_topology():
    result = validate_runtime(_inspect_payload())

    assert result["ok"] is True
    assert result["runtimeUser"] == "99:100"
    assert all(result["checks"].values())


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda p: p[0]["Config"].update({"User": "0:0"}), "runtimeUserNonRoot"),
        (
            lambda p: p[0]["HostConfig"].update({"ReadonlyRootfs": False}),
            "rootFilesystemReadOnly",
        ),
        (
            lambda p: p[0]["HostConfig"].update({"Privileged": True}),
            "notPrivileged",
        ),
        (
            lambda p: p[0]["HostConfig"].update({"SecurityOpt": []}),
            "noNewPrivilegesEnabled",
        ),
        (
            lambda p: p[0]["HostConfig"].update({"CapDrop": []}),
            "allCapabilitiesDropped",
        ),
        (
            lambda p: p[0]["Mounts"].append(
                {"Destination": "/action-books", "RW": True}
            ),
            "writableActionAliasAbsent",
        ),
        (
            lambda p: next(
                item
                for item in p[0]["Mounts"]
                if item["Destination"] == "/books"
            ).update({"RW": True}),
            "ebooksMountedReadOnly",
        ),
    ],
)
def test_runtime_hardening_fails_closed(mutator, expected):
    payload = copy.deepcopy(_inspect_payload())
    mutator(payload)

    with pytest.raises(UpgradeSafetyError, match=expected):
        validate_runtime(payload)


def test_runtime_hardening_requires_one_inspect_object():
    with pytest.raises(UpgradeSafetyError, match="exactly one"):
        validate_runtime([])

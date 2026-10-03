from __future__ import annotations

import copy
from pathlib import Path
import subprocess

import pytest

from tools.upgrade_safety import (
    UpgradeSafetyError,
    validate_clamav_runtime,
    validate_runtime,
)


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
            "writableActionAliasOptedIn",
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


def _clamav_payload() -> list[dict]:
    return [{
        "HostConfig": {
            "Privileged": False,
            "SecurityOpt": ["no-new-privileges:true"],
            "PortBindings": {},
        },
        "Mounts": [
            {
                "Destination": "/var/lib/clamav",
                "Type": "volume",
                "RW": True,
            }
        ],
    }]


def test_clamav_runtime_accepts_private_scanner_topology():
    result = validate_clamav_runtime(_clamav_payload())

    assert result["ok"] is True
    assert all(result["checks"].values())


def test_clamav_runtime_rejects_published_port():
    payload = _clamav_payload()
    payload[0]["HostConfig"]["PortBindings"] = {
        "3310/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3310"}]
    }

    with pytest.raises(UpgradeSafetyError, match="noPublishedPorts"):
        validate_clamav_runtime(payload)


def test_clamav_runtime_rejects_media_mount():
    payload = _clamav_payload()
    payload[0]["Mounts"].append(
        {"Destination": "/books", "Type": "bind", "RW": False}
    )

    with pytest.raises(UpgradeSafetyError, match="noMediaOrConfigMounts"):
        validate_clamav_runtime(payload)


def test_clamav_runtime_requires_signature_database_volume():
    payload = _clamav_payload()
    payload[0]["Mounts"] = []

    with pytest.raises(UpgradeSafetyError, match="signatureDatabasePresent"):
        validate_clamav_runtime(payload)


def test_safer_upgrade_shell_syntax_and_safety_contract():
    completed = subprocess.run(
        ["bash", "-n", "scripts/safer-upgrade.sh"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    script = Path("scripts/safer-upgrade.sh").read_text(encoding="utf-8")
    assert "DEPLOY_BOOKGUARD_UPGRADE" in script
    assert 'branch" != "main"' in script
    assert "compose.clamav.yaml" in script
    assert "up -d --no-build" in script
    assert "No automatic rollback was attempted" in script


def test_writable_ebook_alias_is_accepted_only_when_opted_in_and_same_folder():
    payload = copy.deepcopy(_inspect_payload())
    mounts = payload[0]["Mounts"]
    next(m for m in mounts if m["Destination"] == "/books")["Source"] = "/mnt/user/data/media/books"
    mounts.append({"Destination": "/action-books", "RW": True, "Source": "/mnt/user/data/media/books"})

    with pytest.raises(UpgradeSafetyError, match="writableActionAliasOptedIn"):
        validate_runtime(payload)  # alias mounted, but ebook actions not switched on

    payload[0]["Config"]["Env"] = ["BOOKGUARD_EBOOK_ACTIONS_ENABLED=true"]
    assert validate_runtime(payload)["checks"]["writableActionAliasOptedIn"] is True

    mounts[-1]["Source"] = "/mnt/user/data/other"
    with pytest.raises(UpgradeSafetyError, match="writableActionAliasOptedIn"):
        validate_runtime(payload)  # opted in, but not the same folder as /books

    mounts.pop()
    with pytest.raises(UpgradeSafetyError, match="writableActionAliasOptedIn"):
        validate_runtime(payload)  # opted in, but the writable alias is missing

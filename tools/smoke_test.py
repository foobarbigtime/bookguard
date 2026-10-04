from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


class SmokeTestFailure(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeTestFailure(message)


def _volume_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = payload.get("services")
    if not isinstance(services, dict):
        raise SmokeTestFailure("Compose output has no services object.")
    service = services.get("bookguard")
    if not isinstance(service, dict):
        raise SmokeTestFailure("Compose output has no bookguard service.")
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        raise SmokeTestFailure("BookGuard Compose output has no volume list.")

    mapped: dict[str, dict[str, Any]] = {}
    for volume in volumes:
        if not isinstance(volume, dict):
            raise SmokeTestFailure("Compose did not normalize a volume definition.")
        target = str(volume.get("target") or "")
        if not target:
            raise SmokeTestFailure("A Compose volume has no container target.")
        if target in mapped:
            raise SmokeTestFailure(f"Compose defines duplicate target {target}.")
        mapped[target] = volume
    return mapped


def validate_compose(payload: dict[str, Any], profile: str) -> dict[str, bool]:
    """Validate base and opt-in writable-alias storage topologies."""
    if profile not in {"base", "actions", "malware"}:
        raise SmokeTestFailure(f"Unknown Compose profile: {profile}.")

    volumes = _volume_map(payload)
    services = payload.get("services") or {}
    service = services.get("bookguard") or {}
    security_opt = service.get("security_opt") or []
    cap_drop = service.get("cap_drop") or []
    cap_add = service.get("cap_add") or []
    runtime_user = str(service.get("user") or "").strip()
    runtime_uid = runtime_user.split(":", 1)[0].strip() if runtime_user else ""
    tmpfs = [str(item) for item in (service.get("tmpfs") or [])]
    tmp_mount = next(
        (item for item in tmpfs if item == "/tmp" or item.startswith("/tmp:")),
        "",
    )
    checks = {
        "rootFilesystemReadOnly": bool(service.get("read_only")),
        "tmpfsPresent": bool(tmp_mount),
        "tmpfsNoExec": "noexec" in tmp_mount.split(","),
        "tmpfsNoSuid": "nosuid" in tmp_mount.split(","),
        "tmpfsNoDev": "nodev" in tmp_mount.split(","),
        "tmpfsSizeBounded": "size=" in tmp_mount,
        "noNewPrivilegesEnabled": "no-new-privileges:true" in security_opt,
        "allCapabilitiesDropped": "ALL" in cap_drop,
        "runtimeUserConfigured": bool(runtime_user),
        "runtimeUserNonRoot": runtime_uid not in {"", "0", "root"},
        "noCapabilitiesAdded": not cap_add,
        "writeBypassCapabilityAbsent": "DAC_OVERRIDE" not in cap_add,
        "booksMountedReadOnly": bool(volumes.get("/books", {}).get("read_only")),
        "audiobooksMountedReadOnly": bool(
            volumes.get("/audiobooks", {}).get("read_only")
        ),
        "binderyDatabaseMountedReadOnly": bool(
            volumes.get("/bindery", {}).get("read_only")
        ),
        # BookGuard no longer stages downloads; a writable drop folder is not needed.
        "stagingMountAbsent": "/staging" not in volumes,
        "quarantineMountedWritable": (
            "/quarantine" in volumes
            and not bool(volumes["/quarantine"].get("read_only"))
        ),
        "configMountedWritable": (
            "/config" in volumes and not bool(volumes["/config"].get("read_only"))
        ),
        "quarantineSeparateFromLibrary": (
            volumes.get("/quarantine", {}).get("source")
            != volumes.get("/books", {}).get("source")
        ),
    }

    action = volumes.get("/action-books")
    action_expected = profile == "actions"
    if not action_expected:
        checks["writableActionAliasAbsent"] = action is None
    else:
        checks.update({
            "writableActionAliasPresent": action is not None,
            "writableActionAliasWritable": (
                action is not None and not bool(action.get("read_only"))
            ),
            "actionAliasMapsLibrary": (
                action is not None
                and action.get("source") == volumes.get("/books", {}).get("source")
            ),
        })

    # Direct admission was removed; its writable alias must never come back.
    checks["writableAdmissionAliasAbsent"] = volumes.get("/admission-books") is None

    if profile == "malware":
        clamav = services.get("clamav")
        environment = service.get("environment") or {}
        clamav_volumes = (clamav or {}).get("volumes") or []
        allowed_targets = {"/var/lib/clamav"}
        checks.update({
            "clamavServicePresent": isinstance(clamav, dict),
            "malwareScanEnabledByOverlay": (
                str(environment.get("BOOKGUARD_VERIFICATION_MALWARE_SCAN", "")).lower()
                == "true"
            ),
            "clamdHostUsesPrivateServiceName": (
                environment.get("BOOKGUARD_CLAMD_HOST") == "clamav"
            ),
            "clamavNoPublishedPorts": not bool((clamav or {}).get("ports")),
            "clamavNotPrivileged": not bool((clamav or {}).get("privileged")),
            "clamavNoNewPrivileges": (
                "no-new-privileges:true" in ((clamav or {}).get("security_opt") or [])
            ),
            "clamavHasNoMediaMounts": all(
                isinstance(volume, dict)
                and volume.get("type") == "volume"
                and volume.get("target") in allowed_targets
                for volume in clamav_volumes
            ),
        })

    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise SmokeTestFailure(
            f"Compose {profile} safety checks failed: {', '.join(failed)}"
        )
    return checks


def validate_runtime_rootfs() -> dict[str, bool]:
    """Prove the image root is immutable while the temporary workspace is writable."""
    root_probe = Path("/app/.bookguard-rootfs-write-test")
    tmp_probe = Path("/tmp/bookguard-tmp-write-test")

    root_blocked = False
    try:
        root_probe.write_text("must not be writable", encoding="utf-8")
    except OSError:
        root_blocked = True
    else:
        root_probe.unlink(missing_ok=True)

    tmp_probe.write_text("temporary writes are allowed", encoding="utf-8")
    tmp_writable = tmp_probe.read_text(encoding="utf-8") == "temporary writes are allowed"
    tmp_probe.unlink()

    checks = {
        "applicationRootWriteBlocked": root_blocked,
        "temporaryWorkspaceWritable": tmp_writable,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise SmokeTestFailure(
            f"Runtime root-filesystem checks failed: {', '.join(failed)}"
        )
    return checks


def _read_json_stdin() -> dict[str, Any]:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise SmokeTestFailure(f"Unable to read Compose JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SmokeTestFailure("Compose JSON must be an object.")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated BookGuard smoke tests.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    compose_parser = subparsers.add_parser("compose")
    compose_parser.add_argument(
        "profile",
        choices=("base", "actions", "malware"),
    )
    subparsers.add_parser("rootfs")
    args = parser.parse_args(argv)

    try:
        if args.command == "compose":
            result = {
                "ok": True,
                "profile": args.profile,
                "checks": validate_compose(_read_json_stdin(), args.profile),
            }
        else:
            result = {
                "ok": True,
                "checks": validate_runtime_rootfs(),
            }
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "errorType": type(exc).__name__,
        }, indent=2))
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


class UpgradeSafetyError(RuntimeError):
    pass


def _bookguard_container(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, list) or len(payload) != 1:
        raise UpgradeSafetyError("Expected exactly one docker inspect object.")
    container = payload[0]
    if not isinstance(container, dict):
        raise UpgradeSafetyError("Docker inspect payload is not an object.")
    return container


def validate_runtime(payload: Any) -> dict[str, Any]:
    container = _bookguard_container(payload)
    config = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    mounts = container.get("Mounts") or []

    runtime_user = str(config.get("User") or "").strip()
    uid = runtime_user.split(":", 1)[0].strip() if runtime_user else ""
    tmpfs = host.get("Tmpfs") or {}
    tmp_options = str(tmpfs.get("/tmp") or "")
    security_opt = [str(item) for item in (host.get("SecurityOpt") or [])]
    cap_drop = [str(item).upper() for item in (host.get("CapDrop") or [])]

    mount_map: dict[str, dict[str, Any]] = {}
    for item in mounts:
        if isinstance(item, dict):
            target = str(item.get("Destination") or "")
            if target:
                mount_map[target] = item

    def mounted_rw(target: str) -> bool:
        item = mount_map.get(target)
        return bool(item) and bool(item.get("RW"))

    def mounted_ro(target: str) -> bool:
        item = mount_map.get(target)
        return bool(item) and not bool(item.get("RW"))

    env = {
        str(item).split("=", 1)[0]: str(item).split("=", 1)[1]
        for item in (config.get("Env") or [])
        if "=" in str(item)
    }

    def action_alias_ok() -> bool:
        """The writable ebook alias is allowed only when ebook actions are opted in
        and it is the very same host folder as the read-only /books mount."""
        alias = mount_map.get("/action-books")
        if alias is None:
            return True
        books = mount_map.get("/books") or {}
        return (
            env.get("BOOKGUARD_EBOOK_ACTIONS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
            and bool(alias.get("Source"))
            and alias.get("Source") == books.get("Source")
        )

    checks = {
        "runtimeUserConfigured": bool(runtime_user),
        "runtimeUserNonRoot": uid not in {"", "0", "root"},
        "rootFilesystemReadOnly": bool(host.get("ReadonlyRootfs")),
        "notPrivileged": not bool(host.get("Privileged")),
        "noNewPrivilegesEnabled": any(
            item == "no-new-privileges:true" or item == "no-new-privileges"
            for item in security_opt
        ),
        "allCapabilitiesDropped": "ALL" in cap_drop,
        "tmpfsPresent": bool(tmp_options),
        "tmpfsNoExec": "noexec" in tmp_options.split(","),
        "tmpfsNoSuid": "nosuid" in tmp_options.split(","),
        "tmpfsNoDev": "nodev" in tmp_options.split(","),
        "tmpfsSizeBounded": "size=" in tmp_options,
        "configMountedWritable": mounted_rw("/config"),
        "stagingMountedWritable": mounted_rw("/staging"),
        "quarantineMountedWritable": mounted_rw("/quarantine"),
        "binderyMountedReadOnly": mounted_ro("/bindery"),
        "ebooksMountedReadOnly": mounted_ro("/books"),
        "audiobooksMountedReadOnly": mounted_ro("/audiobooks"),
        "writableActionAliasOptedIn": action_alias_ok(),
        "writableAdmissionAliasAbsent": "/admission-books" not in mount_map,
    }

    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise UpgradeSafetyError(
            "Deployed BookGuard hardening checks failed: " + ", ".join(failed)
        )

    return {
        "ok": True,
        "runtimeUser": runtime_user,
        "checks": checks,
    }



def validate_clamav_runtime(payload: Any) -> dict[str, Any]:
    container = _bookguard_container(payload)
    host = container.get("HostConfig") or {}
    mounts = container.get("Mounts") or []
    security_opt = [str(item) for item in (host.get("SecurityOpt") or [])]
    port_bindings = host.get("PortBindings") or {}

    mount_targets = {
        str(item.get("Destination") or "")
        for item in mounts
        if isinstance(item, dict)
    }
    media_targets = {
        "/books",
        "/audiobooks",
        "/staging",
        "/quarantine",
        "/action-books",
        "/admission-books",
        "/bindery",
        "/config",
    }

    checks = {
        "notPrivileged": not bool(host.get("Privileged")),
        "noNewPrivilegesEnabled": any(
            item == "no-new-privileges:true" or item == "no-new-privileges"
            for item in security_opt
        ),
        "noPublishedPorts": not bool(port_bindings),
        "noMediaOrConfigMounts": not bool(mount_targets & media_targets),
        "signatureDatabasePresent": "/var/lib/clamav" in mount_targets,
        "signatureDatabaseOnly": all(
            target == "/var/lib/clamav" for target in mount_targets
        ),
    }

    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise UpgradeSafetyError(
            "Deployed ClamAV hardening checks failed: " + ", ".join(failed)
        )

    return {
        "ok": True,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate BookGuard's deployed runtime hardening."
    )
    parser.add_argument(
        "command",
        choices=("runtime", "clamav-runtime"),
    )
    args = parser.parse_args(argv)

    try:
        payload = json.load(sys.stdin)
        if args.command == "runtime":
            result = validate_runtime(payload)
        elif args.command == "clamav-runtime":
            result = validate_clamav_runtime(payload)
        else:  # pragma: no cover - argparse owns this branch
            raise UpgradeSafetyError("Unsupported command.")
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "errorType": type(exc).__name__,
                },
                indent=2,
            )
        )
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

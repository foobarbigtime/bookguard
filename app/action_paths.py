from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import ConfigurationError, load_automation_settings, settings
from .file_safety import is_within


class EbookActionSafetyError(RuntimeError):
    pass


def _decode_mount_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def mount_is_writable(path: str | Path) -> bool:
    """Check the effective mount flags instead of trusting root's os.access()."""
    target = Path(path).resolve()
    while not target.exists() and target != target.parent:
        target = target.parent

    best_len = -1
    best_rw = False
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                left = line.split(" - ", 1)[0].split()
                if len(left) < 6:
                    continue
                mount_point = Path(_decode_mount_path(left[4])).resolve()
                try:
                    target.relative_to(mount_point)
                except ValueError:
                    continue
                length = len(str(mount_point))
                if length > best_len:
                    best_len = length
                    best_rw = "rw" in left[5].split(",")
    except OSError:
        return False
    return best_len >= 0 and best_rw


def _relative_ebook_path(local_path: Path, stored_path: Path) -> Path | None:
    try:
        local_relative = local_path.relative_to(Path(settings.ebook_root))
        stored_relative = stored_path.relative_to(
            Path(settings.ebook_bindery_prefix)
        )
    except ValueError:
        return None
    if (
        local_relative != stored_relative
        or not local_relative.parts
        or any(part in {"", ".", ".."} for part in local_relative.parts)
    ):
        return None
    return local_relative


def ebook_action_preview(
    local_path: str,
    stored_path: str,
) -> dict[str, Any]:
    """Prove a writable alias refers to the exact read-only ebook source.

    The scanner/verifier path remains read-only.  A mutation is permitted only
    through a separately configured alias, and only when both names resolve to
    the same device and inode at the preflight boundary.
    """
    try:
        configured = load_automation_settings()
    except ConfigurationError as exc:
        raise EbookActionSafetyError(str(exc)) from exc

    read_root = Path(settings.ebook_root)
    action_root = Path(configured.ebook_action_root)
    local = Path(str(local_path or ""))
    stored = Path(str(stored_path or ""))

    absolute_paths = all(
        path.is_absolute() for path in (read_root, action_root, local, stored)
    )
    relative = _relative_ebook_path(local, stored) if absolute_paths else None
    action_path = action_root / relative if relative is not None else action_root

    read_root_exists = read_root.is_dir() and not read_root.is_symlink()
    action_root_exists = action_root.is_dir() and not action_root.is_symlink()
    source_exists = local.is_file() and not local.is_symlink()
    alias_exists = action_path.is_file() and not action_path.is_symlink()
    same_file = False
    if source_exists and alias_exists:
        try:
            same_file = os.path.samefile(local, action_path)
        except OSError:
            same_file = False

    quarantine_root = Path(settings.quarantine_root).resolve()
    resolved_action_root = (
        action_root.resolve() if action_root_exists else action_root.absolute()
    )
    roots_separate = os.path.normpath(str(read_root)) != os.path.normpath(
        str(action_root)
    )

    checks = {
        "actionsEnabled": settings.allow_actions,
        "ebookActionsEnabled": configured.ebook_actions_enabled,
        "absolutePaths": absolute_paths,
        "readRootExists": read_root_exists,
        "sourceExists": source_exists,
        "pathMappingConfirmed": relative is not None,
        "actionRootSeparate": roots_separate,
        "actionRootExists": action_root_exists,
        "actionRootWritable": action_root_exists
        and mount_is_writable(resolved_action_root),
        "actionAliasExists": alias_exists,
        "actionAliasSameFile": same_file,
        "quarantineOutsideActionRoot": not (
            is_within(quarantine_root, resolved_action_root)
            or is_within(resolved_action_root, quarantine_root)
        ),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "ready": not blockers,
        "checks": checks,
        "blockers": blockers,
        "readOnlyPath": str(local),
        "writablePath": str(action_path),
        "relativePath": relative.as_posix() if relative is not None else None,
        "actionRoot": str(resolved_action_root),
    }


def resolve_writable_ebook_path(local_path: str, stored_path: str) -> Path:
    preview = ebook_action_preview(local_path, stored_path)
    if not preview["ready"]:
        raise EbookActionSafetyError(
            "Writable ebook action preflight failed: "
            + ", ".join(preview["blockers"])
        )
    return Path(preview["writablePath"])

"""Exercise the host restore sizing path with Docker mocked out (no live volumes)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bookguard-backup.sh"


def _run_restore(tmp_path, database_size: int | None, *, symlink: bool = False):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "config"
    state.mkdir()
    bundle = tmp_path / "backup" / "bookguard-test"
    bundle.mkdir(parents=True)
    db = bundle / "bookguard.db"
    if symlink:
        other = tmp_path / "external.db"
        other.write_bytes(b"ok")
        db.symlink_to(other)
    elif database_size is not None:
        with db.open("wb") as handle:
            handle.truncate(database_size)  # sparse file, no giant allocation

    args_path = tmp_path / "captured-args"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "case \"$1\" in\n"
        "  inspect)\n"
        "    case \"$*\" in\n"
        "      *'.Image'* ) printf '%s\\n' 'sha256:fake' ;;\n"
        "      *'.Config.User'* ) printf '%s\\n' '99:100' ;;\n"
        "      *'.Mounts'* ) printf '%s\\n' \"$FAKE_CONFIG\" ;;\n"
        "    esac\n"
        "    ;;\n"
        "  image) exit 0 ;;\n"
        "  run) printf '%s\\n' \"$@\" > \"$FAKE_ARGS\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
           "FAKE_CONFIG": str(state), "FAKE_ARGS": str(args_path)}
    proc = subprocess.run(
        ["bash", str(SCRIPT), "restore-validate", str(bundle)],
        capture_output=True, text=True, env=env, check=False,
    )
    args = args_path.read_text(encoding="utf-8").splitlines() if args_path.exists() else []
    return proc, args


def test_restore_tmpfs_scales_for_database_larger_than_512_mib(tmp_path):
    proc, args = _run_restore(tmp_path, 538_165_248)
    assert proc.returncode == 0, proc.stderr
    assert "Disposable restore tmpfs limit: 1284 MiB" in proc.stdout
    assert "--tmpfs" in args
    assert "/restore:rw,nosuid,nodev,noexec,size=1284m,uid=99,gid=100,mode=0700" in args
    assert "--network" in args and "none" in args
    assert "--read-only" in args
    assert "--cap-drop" in args and "ALL" in args
    assert "/backup-parent:ro" in " ".join(args)


def test_restore_tmpfs_keeps_minimum_for_small_database(tmp_path):
    proc, args = _run_restore(tmp_path, 1_048_576)
    assert proc.returncode == 0, proc.stderr
    assert any("size=512m,uid=99,gid=100" in arg for arg in args)


def test_restore_tmpfs_rejects_missing_or_empty_database(tmp_path):
    for size in (None, 0):
        sub = tmp_path / ("missing" if size is None else "empty")
        sub.mkdir()
        proc, args = _run_restore(sub, size)
        assert proc.returncode != 0
        assert not args


def test_restore_tmpfs_rejects_symlink_database(tmp_path):
    proc, args = _run_restore(tmp_path, None, symlink=True)
    assert proc.returncode != 0
    assert "regular bookguard.db" in proc.stderr
    assert not args

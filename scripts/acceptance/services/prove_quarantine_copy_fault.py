"""Disposable cross-mount fault proof; no Bindery client or live media."""

import json
import os
from pathlib import Path

from app import quarantine_fs
from app.file_safety import sha256_file


source = Path("/books/Unsafe.epub")
control = Path("/books/Unrelated.epub")
destination = Path("/quarantine/Unsafe.epub")
source.write_bytes(b"verified disposable source bytes")
control.write_bytes(b"unrelated disposable control bytes")
expected = sha256_file(source)
control_hash = sha256_file(control)

real_write = os.write
corrupt_writes = 0


def corrupt_copy(fd, data):
    global corrupt_writes
    corrupt_writes += 1
    return real_write(fd, b"X" + bytes(data)[1:])


os.write = corrupt_copy
try:
    try:
        quarantine_fs.move_to_quarantine(
            source, source, destination, expected_sha256=expected
        )
    except quarantine_fs.QuarantineMoveError as exc:
        assert "copy bytes did not match" in str(exc), str(exc)
    else:
        raise AssertionError("The corrupt copy unexpectedly moved the source.")
finally:
    os.write = real_write

assert corrupt_writes > 0, "Separate bind mounts did not exercise the copy path."
assert sha256_file(source) == expected
assert sha256_file(control) == control_hash
assert not destination.exists() and not destination.is_symlink()
assert not list(Path("/quarantine").iterdir()), "Private copy survived the refusal."

quarantine_fs.move_to_quarantine(source, source, destination, expected_sha256=expected)
assert not source.exists() and not source.is_symlink()
assert sha256_file(destination) == expected
quarantine_fs.rollback_quarantine_move(
    source, source, destination, expected_sha256=expected
)
assert sha256_file(source) == expected
assert sha256_file(control) == control_hash
assert not destination.exists() and not list(Path("/quarantine").iterdir())
print(json.dumps({"corruptCopyRefused": True, "sourceRestored": True,
                  "controlUnchanged": True}))

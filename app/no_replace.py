"""Atomic no-replace rename used by publication and quarantine."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path


def rename_no_replace(
    source_directory_fd: int,
    source_name: str,
    destination_directory_fd: int,
    destination_name: str,
    destination: Path,
) -> bool:
    """Rename without replacement, or report that the filesystem lacks support."""
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_directory_fd, os.fsencode(source_name),
        destination_directory_fd, os.fsencode(destination_name), 1,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    if error_number in {errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}:
        return False
    raise OSError(error_number, os.strerror(error_number), destination)

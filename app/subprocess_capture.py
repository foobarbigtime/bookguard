from __future__ import annotations

from dataclasses import dataclass
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Sequence


_READ_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class ProcessOutputLimitExceeded(RuntimeError):
    def __init__(self, stream: str, limit: int):
        self.stream = stream
        self.limit = int(limit)
        super().__init__(f"{stream} exceeded the {limit}-byte output limit")


class ProcessCancelled(RuntimeError):
    """Raised when a caller-requested subprocess cancellation is observed."""


def run_bounded_process(
    cmd: Sequence[str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    cancel_check: Callable[[], bool] | None = None,
) -> BoundedProcessResult:
    """Run a child while enforcing wall-clock and captured-output limits."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if stdout_limit < 0 or stderr_limit < 0:
        raise ValueError("output limits must be non-negative")

    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.stdout is None or proc.stderr is None:
        proc.kill()
        proc.wait()
        raise RuntimeError("failed to create subprocess output pipes")

    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    started = time.monotonic()

    streams = (
        (proc.stdout, "stdout", stdout, int(stdout_limit)),
        (proc.stderr, "stderr", stderr, int(stderr_limit)),
    )

    try:
        for stream, name, _buffer, _limit in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, data=name)

        while selector.get_map():
            if cancel_check and cancel_check():
                raise ProcessCancelled("subprocess cancelled by caller")

            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(list(cmd), timeout)

            events = selector.select(timeout=min(0.25, remaining))
            for key, _mask in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), _READ_CHUNK_BYTES)
                except BlockingIOError:
                    continue

                if not chunk:
                    selector.unregister(key.fileobj)
                    continue

                if key.data == "stdout":
                    target = stdout
                    limit = stdout_limit
                else:
                    target = stderr
                    limit = stderr_limit

                if len(target) + len(chunk) > limit:
                    raise ProcessOutputLimitExceeded(str(key.data), int(limit))
                target.extend(chunk)

        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(list(cmd), timeout)

        returncode = proc.wait(timeout=remaining)
        return BoundedProcessResult(returncode, bytes(stdout), bytes(stderr))
    except BaseException:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        selector.close()
        proc.stdout.close()
        proc.stderr.close()

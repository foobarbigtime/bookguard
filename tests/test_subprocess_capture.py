import sys

import pytest

from app.subprocess_capture import (
    ProcessCancelled,
    ProcessOutputLimitExceeded,
    run_bounded_process,
)


def test_run_bounded_process_stops_on_stdout_limit():
    with pytest.raises(ProcessOutputLimitExceeded) as exc:
        run_bounded_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 8192); sys.stdout.flush()",
            ],
            timeout=5,
            stdout_limit=1024,
            stderr_limit=1024,
        )

    assert exc.value.stream == "stdout"
    assert exc.value.limit == 1024


def test_run_bounded_process_honors_cancellation():
    with pytest.raises(ProcessCancelled):
        run_bounded_process(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            timeout=10,
            stdout_limit=1024,
            stderr_limit=1024,
            cancel_check=lambda: True,
        )

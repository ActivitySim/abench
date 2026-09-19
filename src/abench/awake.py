"""Keep macOS hosts awake for the lifetime of a benchmark CLI invocation."""

import os
import subprocess
import sys
from contextlib import contextmanager


def benchmark_invocation(argv):
    """Exclude reporting, validation, data preparation, and informational commands."""
    return not (
        (argv and argv[0] in {"report", "publish", "validate", "prepare"})
        or any(
            flag in argv
            for flag in ("--help", "-h", "--version", "--report-only", "--allow-sleep")
        )
    )


@contextmanager
def keep_awake(enabled=True):
    if not enabled or sys.platform != "darwin":
        yield
        return
    # -i prevents idle system sleep without keeping the display on. -w also
    # releases the assertion if abench dies without running Python cleanup.
    try:
        process = subprocess.Popen(
            ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise ValueError(
            "Cannot prevent macOS idle sleep with caffeinate. "
            "Use --allow-sleep to run without sleep prevention."
        ) from error
    try:
        # Detect launch failures before committing to an expensive run.
        try:
            code = process.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            print("macOS idle sleep prevention enabled (caffeinate).", flush=True)
        else:
            raise ValueError(
                f"caffeinate exited unexpectedly (exit {code}); "
                "use --allow-sleep to run without sleep prevention."
            )
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

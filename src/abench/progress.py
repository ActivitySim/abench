"""Host-side progress for long commands without changing measured model work."""

import csv
import subprocess
import time


def memory_status(path):
    """Read only the last complete sample; tolerate partial writes and startup."""
    try:
        with path.open("rb") as stream:
            header = stream.readline().decode().strip().split(",")
            start = stream.tell()
            stream.seek(0, 2)
            stream.seek(max(start, stream.tell() - 4096))
            rows = stream.read().decode().split("\n")
        # A final unterminated row may still be in flight from the container.
        row = next(csv.reader([rows[-2]]))
        sample = dict(zip(header, row))
        current = int(sample["current_bytes"]) / 2**30
        peak = int(sample["peak_bytes"]) / 2**30
        return f"; memory {current:.2f} GiB (peak {peak:.2f} GiB)"
    except (OSError, UnicodeError, ValueError, KeyError, IndexError, csv.Error):
        return ""


def run_logged(args, log, interval=15):
    """Keep full logs on disk and print periodic stage status, including failures."""
    label = "Image build" if log.name == "build.log" else log.parent.name
    started = time.monotonic()
    print(f"{label}: started; log: {log}", flush=True)
    with log.open("w") as stream:
        process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT)
        try:
            while True:
                try:
                    code = process.wait(timeout=interval)
                    break
                except subprocess.TimeoutExpired:
                    detail = memory_status(log.parent / "memory.csv")
                    print(
                        f"{label}: {time.monotonic() - started:.0f}s elapsed{detail}",
                        flush=True,
                    )
        except BaseException:
            # Let container_phase's finally block remove a cancelled container.
            # Reap the host Docker client so cancellation never leaves it running.
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    status = "completed" if code == 0 else f"failed (exit {code})"
    print(
        f"{label}: {status} after {time.monotonic() - started:.0f}s"
        + memory_status(log.parent / "memory.csv"),
        flush=True,
    )
    if code:
        raise subprocess.CalledProcessError(code, args)

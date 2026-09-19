"""Host-side progress for long commands without changing measured model work."""

import asyncio
import csv
import os
import subprocess
import sys
import time

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame

from .failures import tail


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


def interactive_terminal():
    return (
        sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
    )


def log_tail(path, lines=12):
    """Bound reads and remove terminal controls from untrusted subprocess output."""
    text = tail(path, limit=16384)
    return (
        "\n".join(
            "".join(char if char.isprintable() or char == "\t" else "" for char in line)
            for line in text.splitlines()[-lines:]
        )
        or "Waiting for log output…"
    )


def live_view(status, log):
    bindings = KeyBindings()

    @bindings.add("c-c")
    @bindings.add("c-d")
    def cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    return Application(
        layout=Layout(
            HSplit(
                [
                    Window(FormattedTextControl(status), height=2, wrap_lines=True),
                    Frame(
                        Window(
                            FormattedTextControl(
                                lambda: log_tail(log),
                                show_cursor=False,
                                get_cursor_position=lambda: Point(
                                    x=0, y=len(log_tail(log).splitlines()) - 1
                                ),
                            ),
                            height=12,
                            wrap_lines=False,
                        ),
                        title=f"Log tail: {log.name}",
                    ),
                ]
            )
        ),
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=True,
        refresh_interval=0.25,
    )


def run_logged(args, log, interval=15):
    """Retain progress on disk and show a live status/log panel on terminals."""
    if interval <= 0:
        raise ValueError("progress interval must be positive")
    label = "Image build" if log.name == "build.log" else log.parent.name
    progress_log = log.with_name(f"{log.stem}.progress.log")
    started = time.monotonic()

    def status():
        return f"{label}: {time.monotonic() - started:.0f}s elapsed" + memory_status(
            log.parent / "memory.csv"
        )

    initial = f"{label}: started; log: {log}; progress: {progress_log}"
    print(initial, flush=True)
    with log.open("w") as stream, progress_log.open("w", buffering=1) as progress:
        progress.write(initial + "\n")
        process = subprocess.Popen(args, stdout=stream, stderr=subprocess.STDOUT)
        try:
            if interactive_terminal():
                app = live_view(status, log)

                async def monitor():
                    try:
                        next_sample = started + interval
                        while process.poll() is None:
                            now = time.monotonic()
                            if now >= next_sample:
                                progress.write(status() + "\n")
                                next_sample = now + interval
                            await asyncio.sleep(min(0.25, interval))
                    except Exception as error:
                        app.exit(exception=error)
                    else:
                        app.exit(result=process.returncode)

                code = app.run(pre_run=lambda: app.create_background_task(monitor()))
            else:
                while True:
                    try:
                        code = process.wait(timeout=interval)
                        break
                    except subprocess.TimeoutExpired:
                        progress.write(status() + "\n")
        except BaseException:
            # Let container_phase's finally block remove a cancelled container.
            # Reap the host Docker client so cancellation never leaves it running.
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            progress.write(f"{status()}; interrupted\n")
            raise
        outcome = "completed" if code == 0 else f"failed (exit {code})"
        final = (
            f"{label}: {outcome} after {time.monotonic() - started:.0f}s"
            + memory_status(log.parent / "memory.csv")
        )
        progress.write(final + "\n")
    print(final, flush=True)
    if code:
        raise subprocess.CalledProcessError(code, args)

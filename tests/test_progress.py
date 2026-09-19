"""Progress is visible without streaming model logs or affecting exit codes."""

import subprocess
import sys

import pytest

from abench.progress import memory_status, run_logged


def test_memory_partial_sample(tmp_path):
    path = tmp_path / "memory.csv"
    assert memory_status(path) == ""
    path.write_text(
        "elapsed_seconds,current_bytes,peak_bytes\n1,1073741824,2147483648\n2,123"
    )
    assert memory_status(path) == "; memory 1.00 GiB (peak 2.00 GiB)"


def test_logged_command_progress_and_exit(tmp_path, capsys):
    log = tmp_path / "build.log"
    run_logged(
        [sys.executable, "-c", "import time; print('build output'); time.sleep(.15)"],
        log,
        interval=0.03,
    )
    output = capsys.readouterr().out
    assert "elapsed" not in output and "completed" in output
    assert "elapsed" in log.with_name("build.progress.log").read_text()
    assert "build output" not in output
    assert "build output" in log.read_text()
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_logged([sys.executable, "-c", "raise SystemExit(7)"], log)
    assert error.value.returncode == 7
    assert "failed (exit 7)" in capsys.readouterr().out


@pytest.mark.parametrize("exit_code", [0, 7])
def test_live_progress(tmp_path, monkeypatch, exit_code):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from abench import progress

    log = tmp_path / "console.log"
    (tmp_path / "memory.csv").write_text(
        "elapsed_seconds,current_bytes,peak_bytes\n1,1073741824,2147483648\n"
    )
    monkeypatch.setattr(progress, "interactive_terminal", lambda: True)
    frames = []
    original = progress.live_view

    def view(status, path):
        app = original(status, path)
        app.before_render += lambda _: frames.append(
            (status(), progress.log_tail(path))
        )
        return app

    monkeypatch.setattr(progress, "live_view", view)
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        args = [
            sys.executable,
            "-u",
            "-c",
            f"import time; print('model output'); time.sleep(.6); exit({exit_code})",
        ]
        if exit_code:
            with pytest.raises(subprocess.CalledProcessError) as error:
                run_logged(args, log, interval=0.03)
            assert error.value.returncode == exit_code
        else:
            run_logged(args, log, interval=0.03)
    assert any("model output" in text for _, text in frames)
    assert any("memory 1.00 GiB (peak 2.00 GiB)" in status for status, _ in frames)
    assert "elapsed" in log.with_name("console.progress.log").read_text()


def test_live_cancellation_reaps_process(tmp_path, monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from abench import progress

    monkeypatch.setattr(progress, "interactive_terminal", lambda: True)
    processes = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(progress.subprocess, "Popen", popen)
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        pipe.send_text("\x03")
        with pytest.raises(KeyboardInterrupt):
            run_logged(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                tmp_path / "console.log",
            )
    assert processes[0].poll() is not None
    assert "interrupted" in (tmp_path / "console.progress.log").read_text()


def test_tail_is_bounded_and_plain(tmp_path):
    from abench.progress import log_tail

    log = tmp_path / "console.log"
    assert log_tail(log) == "Waiting for log output…"
    log.write_text("old\n" * 10000 + "new\x1b[2J\x00\nlast\n")
    result = log_tail(log, lines=2)
    assert result == "new[2J\nlast"

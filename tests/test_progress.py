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
    assert "elapsed" in output and "completed" in output
    assert "build output" not in output
    assert "build output" in log.read_text()
    with pytest.raises(subprocess.CalledProcessError) as error:
        run_logged([sys.executable, "-c", "raise SystemExit(7)"], log)
    assert error.value.returncode == 7
    assert "failed (exit 7)" in capsys.readouterr().out

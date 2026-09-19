"""Sleep inhibition is scoped to benchmark commands and always cleaned up."""

import os
import subprocess
from unittest.mock import Mock

import pytest

from abench import awake, cli


@pytest.mark.parametrize(
    "args,expected",
    [
        ([], True),
        (["."], True),
        (["run", "suite.yaml"], True),
        (["suite.yaml", "--publish-dry-run"], True),
        (["suite.yaml", "--allow-sleep"], False),
        (["--help"], False),
        (["--version"], False),
        (["--report-only"], False),
        (["report"], False),
        (["publish", "output"], False),
        (["validate", "suite.yaml"], False),
        (["prepare", "suite.yaml"], False),
    ],
)
def test_invocations(args, expected):
    assert awake.benchmark_invocation(args) is expected


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_lifetime_and_cleanup(monkeypatch, failure):
    monkeypatch.setattr(awake.sys, "platform", "darwin")
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = [subprocess.TimeoutExpired("caffeinate", 0.05), 0]
    launch = Mock(return_value=process)
    monkeypatch.setattr(awake.subprocess, "Popen", launch)

    def run():
        with awake.keep_awake():
            process.terminate.assert_not_called()
            if failure:
                raise failure()

    if failure:
        with pytest.raises(failure):
            run()
    else:
        run()
    assert launch.call_args.args[0] == [
        "/usr/bin/caffeinate",
        "-i",
        "-w",
        str(os.getpid()),
    ]
    process.terminate.assert_called_once()
    assert process.wait.call_count == 2


@pytest.mark.parametrize("platform,enabled", [("linux", True), ("darwin", False)])
def test_noop(monkeypatch, platform, enabled):
    monkeypatch.setattr(awake.sys, "platform", platform)
    monkeypatch.setattr(
        awake.subprocess, "Popen", lambda *a, **kw: pytest.fail("launched caffeinate")
    )
    with awake.keep_awake(enabled):
        pass


def test_failed_launch(monkeypatch):
    monkeypatch.setattr(awake.sys, "platform", "darwin")
    monkeypatch.setattr(
        awake.subprocess, "Popen", Mock(side_effect=FileNotFoundError())
    )
    with pytest.raises(ValueError, match="--allow-sleep"):
        with awake.keep_awake():
            pytest.fail("benchmark started")


def test_early_exit(monkeypatch):
    monkeypatch.setattr(awake.sys, "platform", "darwin")
    process = Mock()
    process.wait.return_value = 1
    process.poll.return_value = 1
    monkeypatch.setattr(awake.subprocess, "Popen", Mock(return_value=process))
    with pytest.raises(ValueError, match="exited unexpectedly"):
        with awake.keep_awake():
            pytest.fail("benchmark started")


def test_cli_entrypoint_covers_whole_run(monkeypatch):
    from contextlib import contextmanager

    events = []

    @contextmanager
    def inhibit(enabled):
        assert enabled
        events.append("awake")
        try:
            yield
        finally:
            events.append("released")

    monkeypatch.setattr(cli.sys, "argv", ["abench", "suite.yaml"])
    monkeypatch.setattr(cli, "keep_awake", inhibit)
    monkeypatch.setattr(cli, "main", lambda: events.append("run") or 7)
    with pytest.raises(SystemExit) as error:
        cli.entrypoint()
    assert error.value.code == 7
    assert events == ["awake", "run", "released"]

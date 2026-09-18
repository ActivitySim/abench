"""Container failures should identify the model cause and useful artifact paths."""

import json

import pytest

from abench import cli
from abench.failures import BenchmarkFailure, describe_failure


def test_cache_miss_message_names_component_and_remedy(tmp_path):
    phase = tmp_path / "measured"
    phase.mkdir()
    (phase / "cache-miss-1.txt").write_text(
        "/results/cache/flows/flow_ABC/__init__.py\n"
    )
    (phase / "components-1.jsonl").write_text(
        json.dumps({"succeeded": False, "component": "school_escorting"}) + "\n"
    )
    message = describe_failure(tmp_path, "measured")
    assert "school_escorting" in message and "flow_ABC" in message
    assert "Results rejected" in message
    assert str(phase / "console.log") in message
    assert "warmup_households" in message


def test_oom_and_python_exception(tmp_path):
    phase = tmp_path / "warmup"
    phase.mkdir()
    (phase / "docker-state.json").write_text('{"OOMKilled": true, "ExitCode": 137}')
    assert "memory limit" in describe_failure(tmp_path, "warmup")
    (phase / "docker-state.json").write_text('{"ExitCode": 1}')
    (phase / "console.log").write_text(
        "KeyError: missing input column\nSubprocessError: Process worker failed\n"
    )
    assert "KeyError: missing input column" in describe_failure(tmp_path, "warmup")


def test_entrypoint_prints_readable_failure_without_traceback(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "main",
        lambda: (_ for _ in ()).throw(
            BenchmarkFailure("measured failed: cache miss\nLog: example.log")
        ),
    )
    with pytest.raises(SystemExit) as error:
        cli.entrypoint()
    assert error.value.code == 1
    assert "Log: example.log" in capsys.readouterr().err


def test_model_container_error_is_wrapped_with_root_cause(tmp_path, monkeypatch):
    from test_profiles_sources import make_model

    make_model(tmp_path)
    output = tmp_path / "experiment"

    def command(args, log=None):
        if args[:2] == ["docker", "info"]:
            return json.dumps({"OSType": "linux", "CgroupVersion": "2"})
        if "/opt/source-provenance.json" in args:
            return "[]"
        return "test-image"

    def container(spec, output, data, image, phase_name):
        import subprocess

        phase = output / phase_name
        phase.mkdir(parents=True)
        (phase / "cache-miss-1.txt").write_text(
            "/results/cache/flows/flow_ABC/__init__.py\n"
        )
        (phase / "console.log").write_text(
            "ValueError: ordinary model error after compilation\n"
        )
        raise subprocess.CalledProcessError(1, ["docker", "run", "test-image"])

    monkeypatch.setattr(cli, "command", command)
    monkeypatch.setattr(cli, "container_phase", container)
    with pytest.raises(BenchmarkFailure, match="ordinary model error") as error:
        cli.main(
            [
                "run",
                "--model-dir",
                str(tmp_path),
                "--households",
                "2",
                "--no-sharrow",
                "--output-dir",
                str(output),
            ]
        )
    assert "returned non-zero exit status" not in str(error.value)
    assert (output / "report.html").is_file()
    failure = json.loads((output / "experiment.json").read_text())["failure"]
    assert failure["phase"] == "measured"
    assert "ordinary model error" in failure["error"]


def test_successful_exit_with_wrong_sample_is_explained(tmp_path):
    phase = tmp_path / "measured"
    phase.mkdir()
    (tmp_path / "experiment.json").write_text('{"households": 1000}')
    (phase / "docker-state.json").write_text('{"ExitCode": 0}')
    (phase / "output-summary.json").write_text('{"households": {"rows": 500}}')
    message = describe_failure(tmp_path, "measured")
    assert "requested 1000, output contains 500" in message
    assert "Results rejected" in message

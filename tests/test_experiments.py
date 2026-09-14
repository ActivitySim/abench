"""Named suites reuse CLI validation/execution without requiring Docker in tests."""

import json
from pathlib import Path

import pytest
import yaml

from abench import cli, experiments
from abench.experiments import load_suite, run_suite

SHA = "a" * 40
OTHER = "b" * 40


def suite(tmp_path, **updates):
    """Write a portable two-run suite with common paths and source dependencies."""
    document = dict(
        schema_version=1,
        vars={"model": "model", "sample": 4},
        output_root="results-${timestamp}",
        defaults=dict(
            model_dir="${model}",
            profile="sandag",
            data_dir="${model}/data",
            config_overlay=["${model}/chunks"],
            households="${sample}",
            multiprocess=True,
            processes=2,
            sharrow=True,
            sources=[
                f"sharrow=ActivitySim/sharrow@{SHA}",
                f"activitysim=ActivitySim/activitysim@{SHA}",
            ],
        ),
        runs={
            "main": {},
            "pr": {"sources": [f"activitysim=ActivitySim/activitysim@{OTHER}"]},
        },
    )
    document.update(updates)
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def test_defaults_variables_and_source_overrides(tmp_path, monkeypatch):
    path = suite(tmp_path)
    monkeypatch.chdir("/")
    plan = load_suite(path)
    first, second = plan["runs"]
    assert first["name"] == "main"
    assert second["name"] == "pr"
    for run in plan["runs"]:
        args = cli.parser().parse_args(run["argv"])
        assert args.households == 4
        assert args.model_dir == tmp_path / "model"
        assert args.data_dir == tmp_path / "model/data"
        assert args.config_overlay == [tmp_path / "model/chunks"]
        assert f"sharrow=ActivitySim/sharrow@{SHA}" in args.source
    assert f"activitysim=ActivitySim/activitysim@{OTHER}" in second["argv"]
    assert f"activitysim=ActivitySim/activitysim@{SHA}" not in second["argv"]
    assert not Path(plan["output_root"]).exists()


@pytest.mark.parametrize(
    "updates,match",
    [
        ({"vars": {"a": "${b}", "b": "${a}"}}, "cyclic"),
        ({"vars": {}}, "undefined"),
        ({"vars": {"timestamp": "x"}}, "reserved"),
        ({"runs": {"../bad": {}}}, "invalid run name"),
        ({"runs": {}}, "nonempty"),
        ({"runs": {"bad": {"household": 1}}}, "unknown experiment options"),
        ({"runs": {"bad": {"multiprocess": "false"}}}, "YAML boolean"),
        ({"runs": {"bad": {"output_dir": "somewhere"}}}, "unknown experiment options"),
    ],
)
def test_bad_suite_rejected_before_execution(tmp_path, updates, match):
    with pytest.raises(ValueError, match=match):
        load_suite(suite(tmp_path, **updates))


def test_duplicate_yaml_and_existing_root(tmp_path):
    path = suite(tmp_path, output_root="results")
    (tmp_path / "results").mkdir()
    with pytest.raises(ValueError, match="already exists"):
        load_suite(path)
    path.write_text("schema_version: 1\nruns: {}\nruns: {}\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_suite(path)


def test_suite_preflights_all_then_runs_and_reports(tmp_path, monkeypatch):
    path = suite(tmp_path)
    calls, reports = [], []

    def invoke(argv):
        calls.append(argv)
        if argv[0] == "run":
            output = Path(argv[argv.index("--output-dir") + 1])
            output.mkdir()
            (output / "experiment.json").write_text("{}")
        return 0

    monkeypatch.setattr(
        experiments,
        "report",
        lambda paths, destination: reports.append((paths, destination)),
    )
    assert run_suite(path, invoke) == 0
    assert [args[0] for args in calls] == ["validate", "validate", "run", "run"]
    assert len(reports[0][0]) == 2
    root = reports[0][1].parent
    assert (root / "experiments.yaml").read_text() == path.read_text()
    assert len(json.loads((root / "suite.json").read_text())["runs"]) == 2


def test_invalid_later_run_prevents_first_run(tmp_path):
    calls = []

    def invoke(argv):
        calls.append(argv[0])
        if len(calls) == 2:
            raise ValueError("bad input")
        return 0

    with pytest.raises(ValueError, match="bad input"):
        run_suite(suite(tmp_path), invoke)
    assert calls == ["validate", "validate"]
    assert not list(tmp_path.glob("results-*"))


def test_model_failure_stops_suite_and_reports_partial_results(tmp_path, monkeypatch):
    calls, reports = [], []

    def invoke(argv):
        calls.append(argv[0])
        if argv[0] == "run":
            output = Path(argv[argv.index("--output-dir") + 1])
            output.mkdir()
            (output / "experiment.json").write_text("{}")
            raise RuntimeError("model failed")
        return 0

    monkeypatch.setattr(
        experiments, "report", lambda paths, dest: reports.append(paths)
    )
    with pytest.raises(RuntimeError, match="model failed"):
        run_suite(suite(tmp_path), invoke)
    assert calls == ["validate", "validate", "run"]
    assert len(reports[0]) == 1


def test_cli_file_dispatch_and_validation(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        experiments,
        "run_suite",
        lambda path, invoke, validate_only: calls.append((path, validate_only)) or 0,
    )
    path = tmp_path / "named.yaml"
    assert cli.main([str(path)]) == 0
    assert cli.main(["run", str(path)]) == 0
    assert cli.main(["validate", str(path)]) == 0
    assert calls == [(path, False), (path, False), (path, True)]
    with pytest.raises(SystemExit):
        cli.main([str(path), "--households", "5"])


def test_shipped_sandag_suite():
    path = Path(__file__).parents[1] / "examples/sandag-chunked.yaml"
    plan = load_suite(path)
    for run in plan["runs"]:
        args = cli.parser().parse_args(run["argv"])
        assert args.households == 28365
        assert args.warmup_households == 5000
        assert args.processes == 4
        assert args.sharrow
        assert args.data_dir.name == "benchmarking-data"
        assert args.config_overlay[0].name == "configs_explicit_chunk"

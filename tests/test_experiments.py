"""Named suites reuse CLI validation/execution without requiring Docker in tests."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

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


def test_suite_pins_moving_refs_before_execution(tmp_path, monkeypatch):
    calls = []

    def git(argv, **kwargs):
        calls.append(argv[-1])
        sha = SHA if argv[-1] == "refs/heads/main" else OTHER
        return SimpleNamespace(stdout=f"{sha}\t{argv[-1]}\n")

    monkeypatch.setattr(subprocess, "run", git)
    path = suite(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["vars"]["pr"] = 1110
    document["defaults"]["sources"][1] = dict(
        name="activitysim", repository="ActivitySim/activitysim", branch="main"
    )
    document["runs"] = {
        "main": {},
        "main_again": {},
        "pr": {
            "sources": [
                dict(
                    name="activitysim", repository="ActivitySim/activitysim", pr="${pr}"
                )
            ]
        },
    }
    path.write_text(yaml.safe_dump(document))
    executed = []
    monkeypatch.setattr(experiments, "report", lambda *a: None)

    def invoke(argv):
        assert calls == ["refs/heads/main", "refs/pull/1110/head"]
        executed.append(argv)
        return 0

    assert run_suite(path, invoke) == 0
    root = next(tmp_path.glob("results-*"))
    recorded = json.loads((root / "suite.json").read_text())
    assert recorded["source_resolutions"] == [
        dict(repository="ActivitySim/activitysim", ref="refs/heads/main", commit=SHA),
        dict(
            repository="ActivitySim/activitysim",
            ref="refs/pull/1110/head",
            commit=OTHER,
        ),
    ]
    assert "branch: main" in recorded["original_yaml"]
    for run, sha in zip(recorded["runs"], [SHA, SHA, OTHER]):
        assert f"activitysim=ActivitySim/activitysim@{sha}" in run["argv"]
        assert f"sharrow=ActivitySim/sharrow@{SHA}" in run["argv"]
    assert [args[0] for args in executed] == ["validate"] * 3 + ["run"] * 3


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
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    calls = []
    monkeypatch.setattr(
        experiments,
        "run_suite",
        lambda path, invoke, **kwargs: calls.append((path, kwargs)) or 0,
    )
    path = tmp_path / "named.yaml"
    assert cli.main([str(path)]) == 0
    assert cli.main(["run", str(path)]) == 0
    assert cli.main(["validate", str(path)]) == 0
    assert cli.main([str(path), "--set", "activitysim_pr=1110"]) == 0
    assert cli.main(["validate", str(path), "--set=activitysim_pr=1110"]) == 0
    assert cli.main(["prepare", str(path), "--set", "activitysim_pr=1110"]) == 0
    calls = [
        (path, {k: v for k, v in options.items() if k != "interactive"})
        for path, options in calls
    ]
    assert calls == [
        (path, dict(validate_only=False, prepare_only=False, assignments=[])),
        (path, dict(validate_only=False, prepare_only=False, assignments=[])),
        (path, dict(validate_only=True, prepare_only=False, assignments=[])),
        (
            path,
            dict(
                validate_only=False,
                prepare_only=False,
                assignments=["activitysim_pr=1110"],
            ),
        ),
        (
            path,
            dict(
                validate_only=True,
                prepare_only=False,
                assignments=["activitysim_pr=1110"],
            ),
        ),
        (
            path,
            dict(
                validate_only=False,
                prepare_only=True,
                assignments=["activitysim_pr=1110"],
            ),
        ),
    ]
    with pytest.raises(SystemExit):
        cli.main([str(path), "--households", "5"])


def test_pr_override_updates_labels_and_pins_and_main_is_fresh(tmp_path, monkeypatch):
    path = suite(tmp_path)
    document = yaml.safe_load(path.read_text())
    document["inputs"] = {
        "activitysim_pr": {"type": "integer", "default": 100, "minimum": 1}
    }
    document["runs"] = {
        "main": {
            "sources": [
                dict(
                    name="activitysim",
                    repository="ActivitySim/activitysim",
                    branch="main",
                )
            ]
        },
        "pr": {
            "label": "PR ${activitysim_pr}",
            "sources": [
                dict(
                    name="activitysim",
                    repository="ActivitySim/activitysim",
                    pr="${activitysim_pr}",
                )
            ],
        },
    }
    path.write_text(yaml.safe_dump(document))
    calls = []
    main_sha = SHA

    def git(argv, **kwargs):
        calls.append(argv[-1])
        sha = main_sha if argv[-1] == "refs/heads/main" else OTHER
        return SimpleNamespace(stdout=f"{sha}\t{argv[-1]}\n")

    monkeypatch.setattr(subprocess, "run", git)
    first = load_suite(path, assignments=["activitysim_pr=1110"])
    assert first["cli_overrides"] == {"activitysim_pr": 1110}
    assert first["input_values"]["activitysim_pr"] == 1110
    assert (
        yaml.safe_load(first["original_yaml"])["inputs"]["activitysim_pr"]["default"]
        == 100
    )
    assert "PR 1110" in first["runs"][1]["argv"]
    assert f"activitysim=ActivitySim/activitysim@{SHA}" in first["runs"][0]["argv"]
    assert f"activitysim=ActivitySim/activitysim@{OTHER}" in first["runs"][1]["argv"]
    main_sha = "c" * 40
    second = load_suite(path, assignments=["activitysim_pr=1110"])
    assert (
        f"activitysim=ActivitySim/activitysim@{main_sha}" in second["runs"][0]["argv"]
    )
    assert calls == ["refs/heads/main", "refs/pull/1110/head"] * 2
    assert (
        yaml.safe_load(path.read_text())["inputs"]["activitysim_pr"]["default"] == 100
    )


def test_pr_override_requires_declared_variable(tmp_path):
    with pytest.raises(ValueError, match="unknown input"):
        load_suite(suite(tmp_path), assignments=["activitysim_pr=1110"])


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

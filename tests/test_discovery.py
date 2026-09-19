"""Directory selection precedes suite inputs and never runs unselected files."""

import pytest

from abench import cli, experiments
from abench.discovery import instruction_files, select_experiment


def instructions(tmp_path):
    folder = tmp_path / ".abench"
    folder.mkdir()
    for name in ("b.yml", "a.yaml", "ignored.txt"):
        (folder / name).write_text("not loaded during selection")
    (folder / "nested.yaml").mkdir()
    return folder


def test_selection_order(tmp_path, monkeypatch):
    folder = instructions(tmp_path)

    def choose(title, labels):
        assert labels == ["a.yaml", "b.yml"]
        return 1

    monkeypatch.setattr("abench.discovery.choose", choose)
    assert select_experiment(tmp_path, True) == folder / "b.yml"


def test_single_file_still_prompts(tmp_path, monkeypatch):
    folder = instructions(tmp_path)
    (folder / "b.yml").unlink()
    prompts = []
    monkeypatch.setattr(
        "abench.discovery.choose", lambda title, labels: prompts.append(labels) or 0
    )
    assert select_experiment(tmp_path, True) == folder / "a.yaml"
    assert prompts == [["a.yaml"]]
    assert select_experiment(tmp_path, False) == folder / "a.yaml"


def test_noninteractive_multiple_and_missing(tmp_path):
    with pytest.raises(ValueError, match="No experiment directory"):
        instruction_files(tmp_path)
    folder = instructions(tmp_path)
    with pytest.raises(ValueError, match="Multiple experiments"):
        select_experiment(tmp_path, False)
    (folder / "a.yaml").unlink()
    (folder / "b.yml").unlink()
    with pytest.raises(ValueError, match="No YAML"):
        instruction_files(tmp_path)


def test_directory_help_does_not_prompt_or_parse_suites(tmp_path, monkeypatch, capsys):
    instructions(tmp_path)

    def unexpected(*args, **kwargs):
        pytest.fail("help must not prompt or execute")

    monkeypatch.setattr("builtins.input", unexpected)
    monkeypatch.setattr(experiments, "run_suite", unexpected)
    with pytest.raises(SystemExit) as error:
        cli.main([str(tmp_path), "--help"])
    assert error.value.code == 0
    assert "a.yaml" in capsys.readouterr().out


def test_choice_then_inputs_and_file_relative_paths(tmp_path, monkeypatch):
    folder = instructions(tmp_path)
    (folder / "b.yml").write_text("""schema_version: 1
inputs:
  households:
    type: integer
    required: true
output_root: ../results
runs:
  test:
    model_dir: ..
    households: ${households}
""")
    prompts = []
    answers = iter(["42"])
    monkeypatch.setattr(
        "abench.discovery.choose",
        lambda title, labels: prompts.append("Choose experiment") or 1,
    )

    def respond(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", respond)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    plans = []

    def run_suite(path, invoke, **options):
        plans.append(experiments.load_suite(path, interactive=options["interactive"]))
        return 0

    monkeypatch.setattr(experiments, "run_suite", run_suite)
    assert cli.main([str(tmp_path)]) == 0
    assert prompts[0].startswith("Choose experiment")
    assert "households" in prompts[1]
    assert plans[0]["input_values"] == {"households": 42}
    args = plans[0]["runs"][0]["argv"]
    assert args[args.index("--model-dir") + 1] == str(tmp_path)
    assert plans[0]["output_root"] == str(tmp_path / "results")

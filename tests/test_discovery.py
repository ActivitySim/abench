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


def test_selection_order_and_invalid_answers(tmp_path, monkeypatch, capsys):
    folder = instructions(tmp_path)
    assert [p.name for p in instruction_files(tmp_path)] == ["a.yaml", "b.yml"]
    answers = iter(["bad", "0", "3", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert select_experiment(tmp_path, True) == folder / "b.yml"
    assert "Enter a number" in capsys.readouterr().out


def test_single_file_still_prompts_and_accepts_enter(tmp_path, monkeypatch):
    folder = instructions(tmp_path)
    (folder / "b.yml").unlink()
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    assert select_experiment(tmp_path, True) == folder / "a.yaml"
    assert len(prompts) == 1
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


def test_selection_eof(tmp_path, monkeypatch):
    instructions(tmp_path)

    def ended(prompt):
        raise EOFError()

    monkeypatch.setattr("builtins.input", ended)
    with pytest.raises(ValueError, match="nothing started"):
        select_experiment(tmp_path, True)


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
    answers = iter(["2", "42"])

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

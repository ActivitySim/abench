"""Public input validation happens before source resolution or data preparation."""

import subprocess

import pytest
import yaml

from abench import cli, experiments
from abench.inputs import input_schema, resolve_inputs


def test_typed_values_defaults_and_literal_strings():
    document = {
        "inputs": {
            "count": {"type": "integer", "default": 4, "minimum": 0, "maximum": 10},
            "scale": {"type": "number", "default": 1.5},
            "enabled": {"type": "boolean", "default": True},
            "text": {"type": "string", "default": "false"},
        }
    }
    values, overrides = resolve_inputs(
        document, ["count=0", "enabled=false", "text=${count}=yes", "scale=1e-2"]
    )
    assert (
        values
        == overrides
        == dict(count=0, enabled=False, text="${count}=yes", scale=0.01)
    )
    expanded = experiments.expand_variables(
        {**document, "vars": {"label": "value ${text}"}, "result": "${count}"},
        "now",
        values,
    )
    assert expanded["vars"]["label"] == "value ${count}=yes"
    assert expanded["result"] == 0
    assert expanded["inputs"] == document["inputs"]
    defaults, explicit = resolve_inputs(document, [])
    assert defaults == dict(count=4, scale=1.5, enabled=True, text="false")
    assert explicit == {}


@pytest.mark.parametrize(
    "spec,value",
    [
        ({"type": "integer", "default": 1}, "1.5"),
        ({"type": "integer", "default": 1}, "true"),
        ({"type": "number", "default": 1}, "NaN"),
        ({"type": "number", "default": 1}, "inf"),
        ({"type": "boolean", "default": True}, "yes"),
        ({"type": "integer", "default": 1, "minimum": 1}, "0"),
        ({"type": "integer", "default": 1, "maximum": 2}, "3"),
        ({"type": "string", "default": "a", "choices": ["a", "b"]}, "c"),
    ],
)
def test_invalid_overrides(spec, value):
    with pytest.raises(ValueError, match="input x"):
        resolve_inputs({"inputs": {"x": spec}}, [f"x={value}"])


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "integer", "default": True},
        {"type": "number", "default": float("nan")},
        {"type": "string", "default": 1},
        {"type": "integer"},
        {"type": "integer", "required": True, "default": 1},
        {"type": "integer", "required": "true"},
        {"type": "string", "default": "x", "minimum": 1},
        {"type": "integer", "default": 1, "minimum": 2, "maximum": 1},
        {"type": "integer", "default": 1, "choices": [True]},
        {"type": "integer", "default": 1, "choices": []},
        {"type": "integer", "default": 1, "minimum": float("inf")},
        {"type": "integer", "default": 1, "maximum": False},
        {"type": "integer", "default": 1, "typo": 2},
        {"type": "list", "default": []},
    ],
)
def test_bad_schema(spec):
    with pytest.raises(ValueError, match="input x"):
        input_schema({"inputs": {"x": spec}})


@pytest.mark.parametrize(
    "assignments,match",
    [
        (["x=1", "x=2"], "duplicate"),
        (["x"], "NAME=VALUE"),
        (["internal=1"], "unknown input"),
        ([], "missing required input x"),
    ],
)
def test_missing_unknown_duplicate(assignments, match):
    with pytest.raises(ValueError, match=match):
        resolve_inputs(
            {
                "inputs": {"x": {"type": "integer", "required": True}},
                "vars": {"internal": 1},
            },
            assignments,
        )


@pytest.mark.parametrize(
    "document",
    [
        {"inputs": {"timestamp": {"type": "string", "default": "x"}}},
        {"inputs": {"x": {"type": "integer", "default": 1}}, "vars": {"x": 1}},
        {"inputs": {"bad name": {"type": "integer", "default": 1}}},
    ],
)
def test_invalid_names(document):
    with pytest.raises(ValueError):
        input_schema(document)


def test_help_and_invalid_input_never_resolve_sources(tmp_path, monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("unexpected external work")

    monkeypatch.setattr(subprocess, "run", unexpected)
    monkeypatch.setattr(experiments, "prepare_assets", unexpected)
    path = tmp_path / "suite.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "inputs": {
                    "pr": {
                        "type": "integer",
                        "required": True,
                        "minimum": 1,
                        "description": "PR to benchmark",
                    }
                },
                "output_root": "results",
                "runs": {
                    "main": {
                        "sources": [
                            {
                                "name": "activitysim",
                                "repository": "ActivitySim/activitysim",
                                "branch": "main",
                            }
                        ]
                    }
                },
            }
        )
    )
    with pytest.raises(SystemExit) as error:
        cli.main([str(path), "--help"])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "pr (integer; required)" in help_text
    assert "PR to benchmark" in help_text
    assert "minimum: 1" in help_text
    for extra in ([], ["--set", "pr=0"], ["--set", "typo=1"]):
        with pytest.raises(ValueError):
            cli.main([str(path), *extra])
    assert not (tmp_path / "results").exists()


def test_interactive_defaults_required_and_validation(monkeypatch, capsys):
    answers = iter(["", "", "bad", "0", "1110", "", ""])
    prompts = []

    def respond(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", respond)
    values, explicit = resolve_inputs(
        {
            "inputs": {
                "households": {"type": "integer", "default": 500000},
                "pr": {"type": "integer", "required": True, "minimum": 1},
                "sharrow": {"type": "boolean", "default": True},
                "label": {"type": "string", "default": ""},
            }
        },
        [],
        interactive=True,
    )
    assert values == dict(households=500000, pr=1110, sharrow=True, label="")
    assert explicit == {}
    assert "[500000]" in prompts[0]
    assert "(required)" in prompts[1]
    output = capsys.readouterr().out
    assert "pr is required" in output and "Try again" in output


def test_prompt_eof_cancels(monkeypatch):
    def ended(prompt):
        raise EOFError()

    monkeypatch.setattr("builtins.input", ended)
    with pytest.raises(ValueError, match="experiment not started"):
        resolve_inputs(
            {"inputs": {"x": {"type": "integer", "required": True}}},
            [],
            interactive=True,
        )


def test_explicit_values_skip_prompts(monkeypatch):
    def unexpected(prompt):
        pytest.fail("explicit input should not prompt")

    monkeypatch.setattr("builtins.input", unexpected)
    assert resolve_inputs(
        {"inputs": {"x": {"type": "integer", "required": True}}},
        ["x=1"],
        interactive=True,
    )[0] == {"x": 1}


def test_cli_terminal_detection(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        experiments, "run_suite", lambda *a, **kw: calls.append(kw) or 0
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    path = str(tmp_path / "suite.yaml")
    cli.main([path])
    cli.main([path, "--non-interactive"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    cli.main([path])
    assert [call["interactive"] for call in calls] == [True, False, False]


def test_prompted_inputs_are_saved_before_execution(tmp_path, monkeypatch):
    import json

    path = tmp_path / "suite.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "inputs": {"households": {"type": "integer", "default": 4}},
                "output_root": "results",
                "defaults": {"households": "${households}"},
                "runs": {"test": {}},
            }
        )
    )
    answers = []
    monkeypatch.setattr("builtins.input", lambda prompt: answers.append(prompt) or "5")
    monkeypatch.setattr(experiments, "report", lambda *a: None)
    calls = []

    def invoke(argv):
        assert len(answers) == 1
        assert argv[argv.index("--households") + 1] == "5"
        calls.append(argv[0])
        return 0

    experiments.run_suite(path, invoke, interactive=True)
    plan = json.loads((tmp_path / "results/suite.json").read_text())
    assert plan["input_values"] == {"households": 5}
    assert plan["cli_overrides"] == {}
    assert calls == ["validate", "run"]

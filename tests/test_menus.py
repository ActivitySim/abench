"""Exercise real arrow-key bindings without needing a physical terminal."""

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from abench.inputs import resolve_inputs
from abench.menus import choose


@pytest.mark.parametrize(
    "keys,initial,expected",
    [
        ("\r", 1, 1),
        ("\x1b[B\r", 0, 1),
        ("\x1b[A\r", 1, 0),
        ("\x1b[A\r", 0, 2),
        ("\x1b[B\r", 2, 0),
    ],
)
def test_navigation(keys, initial, expected):
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        pipe.send_text(keys)
        assert choose("Mode", ["First", "Second", "Third"], initial) == expected


@pytest.mark.parametrize(
    "key,error", [("\x03", KeyboardInterrupt), ("\x04", ValueError)]
)
def test_cancellation(key, error):
    with (
        create_pipe_input() as pipe,
        create_app_session(input=pipe, output=DummyOutput()),
    ):
        pipe.send_text(key)
        with pytest.raises(error):
            choose("Mode", ["First"])


def test_typed_choice_defaults_and_required(monkeypatch):
    calls = []

    def pick(title, labels, initial):
        calls.append((labels, initial))
        return initial

    monkeypatch.setattr("abench.inputs.choose", pick)
    values, _ = resolve_inputs(
        {
            "inputs": {
                "count": {"type": "integer", "choices": [2, 4, 8], "default": 4},
                "mode": {"type": "string", "choices": ["a", "b"], "required": True},
            }
        },
        [],
        interactive=True,
    )
    assert calls == [(["2", "4", "8"], 1), (["a", "b"], 0)]
    assert values == {"count": 4, "mode": "a"}

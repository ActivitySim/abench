"""Moving suite selectors resolve to immutable, reproducible build inputs."""

import subprocess
from types import SimpleNamespace

import pytest

from abench.sources import source, suite_source

SHA = "a" * 40


def test_branch_and_pr_heads_resolve_once(monkeypatch):
    calls = []

    def git(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=f"{SHA}\t{argv[-1]}\n")

    monkeypatch.setattr(subprocess, "run", git)
    memo = {}
    base = dict(
        name="My_Addon", repository="Org/Repo", extras=["fast"], subdirectory="python"
    )
    branch = suite_source(base | {"branch": "feature/test"}, memo)
    assert branch == source(base | {"commit": SHA})
    assert suite_source(base | {"branch": "feature/test"}, memo) == branch
    suite_source(base | {"pr": 1110}, memo)
    assert [args[-1] for args in calls] == [
        "refs/heads/feature/test",
        "refs/pull/1110/head",
    ]
    assert calls[1][-2] == "https://github.com/Org/Repo.git"
    assert len(memo) == 2


@pytest.mark.parametrize(
    "fields",
    [
        {"branch": "main", "commit": SHA},
        {"branch": "main", "pr": 1},
        {"pr": True},
        {"pr": "1110"},
        {"pr": 0},
        {"pr": -1},
        {"branch": "*"},
        {"branch": "../main"},
        {"branch": "main\n"},
        {"branch": "main", "repository": "https://evil.example/repo"},
        {"branch": "main", "typo": 1},
    ],
)
def test_invalid_selector_never_contacts_github(monkeypatch, fields):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid source reached Git")

    monkeypatch.setattr(subprocess, "run", unexpected)
    with pytest.raises(ValueError):
        suite_source(
            dict(name="activitysim", repository="ActivitySim/activitysim") | fields, {}
        )


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("git"),
        subprocess.TimeoutExpired("git", 60),
        subprocess.CalledProcessError(2, "git"),
    ],
)
def test_resolution_failure_explains_source(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ValueError, match="Cannot resolve Org/Repo refs/pull/1110/head"):
        suite_source(dict(name="addon", repository="Org/Repo", pr=1110), {})


def test_unexpected_ref_response_rejected(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=f"{SHA}\trefs/pull/1/merge\n"),
    )
    with pytest.raises(ValueError, match="exact commit"):
        suite_source(dict(name="addon", repository="Org/Repo", pr=1), {})


def test_exact_pin_does_not_need_git(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("exact pin reached Git")

    monkeypatch.setattr(subprocess, "run", unexpected)
    assert suite_source(f"addon=Org/Repo@{SHA}", {})["commit"] == SHA

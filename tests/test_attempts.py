"""Completed compilation attempts retry; ordinary failures never do."""

import json
import subprocess

import pytest

from abench.attempts import measured_attempts
from abench.common import write_json
from abench.failures import BenchmarkFailure
from abench.report import load_run


def run_fixture(tmp_path, misses, retries=2, failure=None):
    """Provide real report artifacts while faking only the container boundary."""
    spec = dict(schema_version=2, cache_retries=retries, households=1)
    calls, publications = [], []

    def run(phase_name):
        assert not (tmp_path / "cache/model").exists()
        (tmp_path / "cache/model").mkdir(parents=True)
        phase = tmp_path / phase_name
        phase.mkdir(parents=True)
        calls.append(True)
        if failure == "process":
            raise subprocess.CalledProcessError(1, "docker")
        write_json(phase / "status.json", dict(returncode=0))
        write_json(phase / "docker-state.json", dict(ExitCode=0, OOMKilled=False))
        write_json(
            phase / "output-summary.json",
            dict(households=dict(rows=2 if failure else 1)),
        )
        (phase / "components-1.jsonl").write_text(
            json.dumps(dict(component="test", seconds=len(calls), succeeded=True))
            + "\n"
        )
        (phase / "memory.csv").write_text(
            "elapsed_seconds,current_bytes,peak_bytes\n0,1,1\n"
        )
        if misses[len(calls) - 1]:
            (phase / "cache-miss-1.txt").write_text(
                "/results/cache/flows/flow_a/__init__.py\n"
            )

    measured_attempts(spec, tmp_path, run, lambda: publications.append(True))
    return spec, calls, publications


def test_preparation_excluded_and_clean_attempt_accepted(tmp_path):
    spec, calls, publications = run_fixture(tmp_path, [True, False])
    assert len(calls) == len(publications) == 2
    assert [a["status"] for a in spec["attempts"]] == ["cache preparation", "accepted"]
    assert (tmp_path / "attempts/attempt-001/cache-miss-1.txt").exists()
    assert load_run(tmp_path)["valid"]
    assert load_run(tmp_path)["components"]["test"]["mean"] == 2


@pytest.mark.parametrize("retries", [0, 2])
def test_persistent_compilation_exhausts_budget(tmp_path, retries):
    with pytest.raises(BenchmarkFailure, match="No valid benchmark"):
        run_fixture(tmp_path, [True] * (retries + 1), retries)
    spec = json.loads((tmp_path / "experiment.json").read_text())
    assert len(spec["attempts"]) == retries + 1
    assert not load_run(tmp_path)["valid"]


@pytest.mark.parametrize("failure", ["process", "sample"])
def test_ordinary_failure_not_retried_even_with_misses(tmp_path, failure):
    with pytest.raises((BenchmarkFailure, subprocess.CalledProcessError)):
        run_fixture(tmp_path, [True], failure=failure)
    spec = json.loads((tmp_path / "experiment.json").read_text())
    assert len(spec["attempts"]) == 1
    assert spec["attempts"][0]["status"] == "failed"


def test_retry_options_in_yaml(tmp_path):
    from abench.cli import parser
    from abench.experiments import arguments

    assert parser().parse_args([]).cache_retries == 2
    assert (
        parser().parse_args(arguments({"cache_retries": 4}, tmp_path)).cache_retries
        == 4
    )

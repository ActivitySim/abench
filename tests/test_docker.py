"""Opt-in integration through the real Docker supervisor and ActivitySim workflow."""

import json
import os
import shutil
from pathlib import Path

import pytest

from abench import cli
from abench.report import load_run

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get("ABENCH_DOCKER_TESTS") != "1",
        reason="set ABENCH_DOCKER_TESTS=1 to run Docker integration",
    ),
]
ACTIVITYSIM = "5c6fae24a91a57a2d6dfc2e1dbe062a61d94545a"
SHARROW = "fc175b27d8e0c5d202721c67d96b050e6117b235"


@pytest.mark.parametrize("multiprocess,sharrow", [(False, False), (True, True)])
def test_tiny_model(tmp_path, multiprocess, sharrow):
    """Build pinned sources, run a full warmup where enabled, then measure."""
    root = tmp_path / "model"
    shutil.copytree(Path(__file__).parent / "fixtures/tiny", root)
    output = tmp_path / "experiment"
    args = [
        "run",
        "--model-dir",
        str(root),
        "--source",
        f"activitysim=ActivitySim/activitysim@{ACTIVITYSIM}",
        "--source",
        f"sharrow=ActivitySim/sharrow@{SHARROW}",
        "--households",
        "4",
        "--memory",
        "3g",
        "--shm-size",
        "256m",
        "--output-dir",
        str(output),
    ]
    args += (
        ["--multiprocess", "--processes", "2"] if multiprocess else ["--single-process"]
    )
    args += ["--sharrow"] if sharrow else ["--no-sharrow"]
    assert cli.main(args) == 0
    run = load_run(output)
    assert run["valid"]
    assert run["components"]["bench_compute"]["n"] == (2 if multiprocess else 1)
    assert run["outputs"]["households"]["rows"] == 4
    assert run["memory"] and all(row["current_bytes"] > 0 for row in run["memory"])
    assert (output / "warmup").exists() == sharrow
    assert len(json.loads((output / "source-provenance.json").read_text())) == 2
    assert not list((output / "measured").glob("cache-miss-*"))
    lines = (output / "measured/output/final_households.csv").read_text().splitlines()
    assert set(lines[1:]) == {"1,20", "2,40", "3,60", "4,80"}

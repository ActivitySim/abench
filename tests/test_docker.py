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


@pytest.mark.parametrize(
    "multiprocess,sharrow,retry",
    [(False, False, False), (True, True, False), (True, True, True)],
)
def test_tiny_model(tmp_path, multiprocess, sharrow, retry):
    """Build pinned sources, run a full warmup where enabled, then measure."""
    root = tmp_path / "model"
    shutil.copytree(Path(__file__).parent / "fixtures/tiny", root)
    if retry:
        (root / "force-measured-signature").touch()
        profile = root / "benchmark.yaml"
        import yaml

        settings = yaml.safe_load(profile.read_text())
        settings["snapshot"].append("force-measured-signature")
        profile.write_text(yaml.safe_dump(settings))
    output = tmp_path / "experiment"
    args = [
        "run",
        "--model-dir",
        str(root),
        "--source",
        f"activitysim=ActivitySim/activitysim@{ACTIVITYSIM}",
        "--source",
        f"sharrow=ActivitySim/sharrow@{SHARROW}",
        "--flow-cache-dir",
        str(tmp_path / "shared-flows"),
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
    attempts = run["spec"]["attempts"]
    assert len(attempts) == (2 if retry else 1)
    assert attempts[-1]["status"] == "accepted"
    assert attempts[-1]["compilations"] == 0
    if retry:
        assert attempts[0]["status"] == "cache preparation"
        archived = output / attempts[0]["directory"]
        assert list(archived.glob("cache-miss-*.txt"))
        assert json.loads((archived / "status.json").read_text())["returncode"] == 0
        assert (archived / "output/final_households.csv").exists()
    assert run["components"]["bench_compute"]["n"] == (2 if multiprocess else 1)
    assert run["outputs"]["households"]["rows"] == 4
    assert run["memory"] and all(row["current_bytes"] > 0 for row in run["memory"])
    assert (output / "warmup").exists() == sharrow
    if sharrow:
        warmup = json.loads((output / "warmup/effective-settings.json").read_text())
        assert warmup["households_sample_size"] == 4
        assert warmup["multiprocess"] is False
        assert warmup["num_processes"] == 1
    assert len(json.loads((output / "source-provenance.json").read_text())) == 2
    assert not list((output / "measured").glob("cache-miss-*"))
    lines = (output / "measured/output/final_households.csv").read_text().splitlines()
    assert set(lines[1:]) == {"1,20", "2,40", "3,60", "4,80"}
    if retry:
        # The signature prepared by measurement must survive in the shared cache.
        repeated = tmp_path / "repeat"
        args[args.index("--output-dir") + 1] = str(repeated)
        assert cli.main(args) == 0
        reused = load_run(repeated)
        assert reused["valid"]
        assert len(reused["spec"]["attempts"]) == 1
        assert reused["spec"]["flow_cache"]["restored_files"] > 0
        assert "cache preparation" in (output / "report.html").read_text()


def test_named_suite(tmp_path):
    """Exercise file dispatch, shared defaults, serial/MP overrides, and comparison."""
    import yaml

    root = tmp_path / "model"
    shutil.copytree(Path(__file__).parent / "fixtures/tiny", root)
    path = tmp_path / "experiments.yaml"
    path.write_text(
        yaml.safe_dump(
            dict(
                schema_version=1,
                output_root="results",
                defaults=dict(
                    model_dir="model",
                    flow_cache_dir="shared-flows",
                    sharrow=True,
                    households=4,
                    warmup_households=2,
                    memory="3g",
                    shm_size="256m",
                    sources=[
                        f"activitysim=ActivitySim/activitysim@{ACTIVITYSIM}",
                        f"sharrow=ActivitySim/sharrow@{SHARROW}",
                    ],
                ),
                runs={"serial": {}, "parallel": {"multiprocess": True, "processes": 2}},
            ),
            sort_keys=False,
        )
    )
    assert cli.main([str(path)]) == 0
    root = tmp_path / "results"
    runs = json.loads((root / "comparison.json").read_text())
    assert len(runs) == 2 and all(run["valid"] for run in runs)
    assert [run["components"]["bench_compute"]["n"] for run in runs] == [1, 2]
    assert (root / "experiments.yaml").read_text() == path.read_text()
    assert (root / "suite.json").is_file()
    for name in ("serial", "parallel"):
        warmup = json.loads(
            (root / name / "warmup/effective-settings.json").read_text()
        )
        assert warmup["households_sample_size"] == 2
        assert warmup["multiprocess"] is False
        assert warmup["num_processes"] == 1
        summaries = json.loads((root / name / "warmup/output-summary.json").read_text())
        assert summaries["households"]["rows"] == 2

    # Both warmups execute, but the second must load compiled code from the first.
    serial = json.loads((root / "serial/experiment.json").read_text())["flow_cache"]
    parallel = json.loads((root / "parallel/experiment.json").read_text())["flow_cache"]
    assert serial["restored_files"] == 0
    assert parallel["restored_files"] > 0
    assert serial["key"] == parallel["key"]
    assert (root / "serial/warmup/output/flow-compiled.txt").exists()
    assert not (root / "parallel/warmup/output/flow-compiled.txt").exists()

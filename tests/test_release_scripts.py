"""Local checks for the manual full-scale release helpers."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parents[1] / "release" / "aws"


def load_population_check():
    spec = importlib.util.spec_from_file_location(
        "check_full_population", HERE / "check_full_population.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def summaries(root, input_rows, output_rows):
    phase = root / "measured"
    phase.mkdir(parents=True, exist_ok=True)
    (phase / "input-summary.json").write_text(
        json.dumps({"households": {"rows": input_rows}})
    )
    (phase / "output-summary.json").write_text(
        json.dumps({"households": {"rows": output_rows}})
    )


def test_full_population_check(tmp_path, capsys):
    checker = load_population_check()
    summaries(tmp_path, 10, 10)
    assert checker.main([str(tmp_path)]) == 0
    assert "10 households" in capsys.readouterr().out

    summaries(tmp_path, 10, 9)
    assert checker.main([str(tmp_path)]) == 1
    assert "input=10, output=9" in capsys.readouterr().out


def test_release_shell_scripts_parse():
    subprocess.run(
        ["bash", "-n", HERE / "launch.sh", HERE / "run-model.sh"], check=True
    )
    for script in ("launch.sh", "run-model.sh"):
        result = subprocess.run(
            [HERE / script, "--help"], capture_output=True, check=True, text=True
        )
        assert "full-scale ActivitySim release" in result.stdout


def test_population_check_rejects_missing_summary(tmp_path):
    result = subprocess.run(
        [sys.executable, HERE / "check_full_population.py", tmp_path],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "cannot read household rows" in result.stderr


def test_launcher_resolves_default_model_branches(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        """#!/usr/bin/env bash
case " $* " in
  *" rev-parse HEAD "*) printf '%s\\n' 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' ;;
  *" status --porcelain "*) ;;
  *" refs/heads/extended "*) printf '%s\\t%s\\n' 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' 'refs/heads/extended' ;;
  *" refs/heads/main "*) printf '%s\\t%s\\n' 'cccccccccccccccccccccccccccccccccccccccc' 'refs/heads/main' ;;
  *) exit 2 ;;
esac
"""
    )
    git.chmod(0o755)
    aws = bin_dir / "aws"
    aws.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >>"$AWS_LOG"
if [[ " $* " == *" cloudformation describe-stacks "* ]]; then
  printf '%s\\n' i-test
fi
"""
    )
    aws.chmod(0o755)
    log = tmp_path / "aws.log"
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", AWS_LOG=str(log))
    sha = "d" * 40
    result = subprocess.run(
        [
            HERE / "launch.sh",
            "--bucket",
            "test-bucket",
            "--vpc-id",
            "vpc-test",
            "--subnet-id",
            "subnet-test",
            "--activitysim-commit",
            sha,
            "--sharrow-commit",
            sha,
            "--no-wait",
        ],
        capture_output=True,
        env=env,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    deployments = [line for line in calls if "cloudformation deploy" in line]
    assert len(deployments) == 2
    assert any(
        "Model=mtc-extended" in line and f"ModelCommit={'b' * 40}" in line
        for line in deployments
    )
    assert any(
        "Model=sandag" in line and f"ModelCommit={'c' * 40}" in line
        for line in deployments
    )

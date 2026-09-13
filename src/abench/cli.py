"""Run reproducible ActivitySim benchmarks with model profiles."""

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .common import read_json, write_json
from .profiles import load_profile, validate_model
from .report import load_run, report
from .sources import resolve_sources

PACKAGE = Path(__file__).resolve().parent


def command(args, log=None):
    """Keep build/run output on disk and propagate failures to the caller."""
    if log:
        with log.open("w") as stream:
            subprocess.run(args, stdout=stream, stderr=subprocess.STDOUT, check=True)
    else:
        return subprocess.check_output(args, text=True).strip()


def git_info(root, args):
    """Non-Git model directories are supported; snapshots still capture their files."""
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else None


def commit(value):
    if not re.fullmatch(r"[0-9a-fA-F]{40}", value):
        raise argparse.ArgumentTypeError("provide the full 40-character Git commit SHA")
    return value.lower()


def positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return value


def parser():
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog="Named experiments: abench experiments.yaml; preflight: abench validate experiments.yaml",
    )
    p.add_argument("--model-dir", type=Path, default=Path.cwd())
    p.add_argument("--profile", default="benchmark.yaml")
    p.add_argument(
        "--source",
        action="append",
        default=[],
        help="distribution=organization/repository@40-character-SHA",
    )
    p.add_argument("--activitysim-commit", type=commit)
    p.add_argument("--sharrow-commit", type=commit)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--single-process", dest="multiprocess", action="store_false")
    mode.add_argument("--multiprocess", action="store_true")
    p.set_defaults(multiprocess=False)
    p.add_argument("--processes", type=int, help="required for --multiprocess")
    p.add_argument("--sharrow", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument(
        "--households", type=int, default=1000, help="0 means full population"
    )
    p.add_argument("--data-dir", type=Path, default=None)
    p.add_argument(
        "--config-overlay",
        type=Path,
        nargs="+",
        default=[],
        help="extra config directories, highest priority first",
    )
    p.add_argument(
        "--cache-from",
        type=Path,
        help="seed flow cache from an experiment with the same revisions and dependencies",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        required=False,
        help="new experiment directory, or report HTML with --report-only",
    )
    p.add_argument("--label", help="experiment label in comparisons")
    p.add_argument(
        "--compare",
        type=Path,
        nargs="+",
        default=[],
        help="previous experiment directories",
    )
    p.add_argument(
        "--report-only",
        action="store_true",
        help="rebuild a comparison of --compare directories without Docker",
    )
    p.add_argument(
        "--interval",
        type=positive,
        default=0.5,
        help="memory sampling interval in seconds",
    )
    p.add_argument(
        "--memory", default="16g", help="Docker memory and memory+swap limit"
    )
    p.add_argument(
        "--shm-size", default="8g", help="/dev/shm capacity; charged against --memory"
    )
    p.add_argument(
        "--platform",
        choices=("linux/arm64", "linux/amd64"),
        help="defaults to Docker native architecture",
    )
    return p


def mount(source, target, readonly=False):
    source = str(source.resolve())
    if "," in source:
        raise ValueError("Docker bind paths cannot contain commas")
    return [
        "--mount",
        f"type=bind,src={source},dst={target}" + (",readonly" if readonly else ""),
    ]


def container_phase(spec, output, data, image, phase_name):
    """Retain Docker exit/OOM state even when the supervisor cannot finish."""
    phase = output / phase_name
    phase.mkdir()
    name = "abench-" + uuid.uuid4().hex[:12]
    args = [
        "docker",
        "run",
        "--name",
        name,
        "--cgroupns=private",
        "--memory",
        spec["memory"],
        "--memory-swap",
        spec["memory"],
        "--shm-size",
        spec["shm_size"],
        "--network=none",
    ]
    if spec["platform"]:
        args += ["--platform", spec["platform"]]
    args += mount(output / "model", "/model", True)
    args += mount(output / "runner", "/benchmark", True)
    args += mount(data, "/data", True)
    args += mount(output, "/results")
    args += [image, "supervise", f"/results/{phase_name}"]
    try:
        command(args, phase / "console.log")
    finally:
        try:
            state = json.loads(
                command(["docker", "inspect", name, "--format", "{{json .State}}"])
            )
            write_json(phase / "docker-state.json", state)
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def main(argv=None):
    p = parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # A file invocation stays separate from model profiles and ordinary flags.
    candidate = argv[1:] if argv and argv[0] in ("run", "validate") else argv
    if (
        candidate
        and not candidate[0].startswith("-")
        and candidate[0] not in ("run", "report", "validate")
    ):
        if len(candidate) != 1:
            p.error(
                "an experiment file cannot be mixed with command-line overrides; edit its defaults or runs"
            )
        from .experiments import run_suite

        return run_suite(Path(candidate[0]), main, validate_only=argv[0] == "validate")
    action = argv.pop(0) if argv and argv[0] in ("run", "report", "validate") else "run"
    args = p.parse_args(argv)
    if action == "report":
        args.report_only = True
    if action != "validate" and args.output_dir is None:
        p.error("--output-dir is required")
    comparisons = [path.expanduser().resolve() for path in args.compare]
    for previous in comparisons:
        load_run(previous)
    output = args.output_dir.expanduser().resolve() if args.output_dir else None
    if args.report_only:
        if output is None:
            p.error("--output-dir is required for reporting")
        if not comparisons:
            p.error("--report-only requires --compare")
        report(comparisons, output)
        print(output)
        return 0
    root = args.model_dir.expanduser().resolve()
    try:
        profile = load_profile(args.profile, root)
        sources = resolve_sources(
            profile.get("sources", []),
            args.source,
            args.activitysim_commit,
            args.sharrow_commit,
        )
        data = (
            args.data_dir.expanduser().resolve()
            if args.data_dir
            else (root / profile.get("data_dir", "data")).resolve()
        )
        validate_model(profile, root, data, args.households)
    except ValueError as error:
        p.error(str(error))
    args.activitysim_commit = next(
        s["commit"] for s in sources if s["name"] == "activitysim"
    )
    args.sharrow_commit = next(
        (s["commit"] for s in sources if s["name"] == "sharrow"), None
    )
    if args.sharrow and not args.sharrow_commit:
        p.error("Sharrow enabled: provide a sharrow source override")
    if args.households < 0:
        p.error("--households must be nonnegative")
    if args.multiprocess and (args.processes is None or args.processes < 1):
        p.error("--multiprocess requires --processes >= 1")
    if not args.multiprocess and args.processes not in (None, 1):
        p.error("--processes > 1 requires --multiprocess")
    for value in (args.memory, args.shm_size):
        if not re.fullmatch(r"[1-9][0-9]*[bkmgBKMG]?", value):
            p.error("memory sizes must be positive integer Docker sizes, such as 16g")
    args.data_dir = data
    overlays = [(root / path.expanduser()).resolve() for path in args.config_overlay]
    for path in overlays:
        if not path.is_dir() or (output is not None and output.is_relative_to(path)):
            p.error("config overlays must be existing directories outside --output-dir")
    seed = args.cache_from.expanduser().resolve() if args.cache_from else None
    if seed:
        prior = read_json(seed / "experiment.json", {})
        for key in ("activitysim_commit", "sharrow_commit"):
            if prior.get(key) != getattr(args, key):
                p.error(f"--cache-from must use the same {key}")
        if not (seed / "cache/flows").is_dir():
            p.error("--cache-from has no flow cache")
    for source in profile["snapshot"]:
        if output is not None and output.is_relative_to(root / source):
            p.error("--output-dir must be outside snapshot source directories")
    if seed and prior.get("sources") != sources:
        p.error("--cache-from must use the same complete source dependency manifest")
    if "," in str(output) or "," in str(data):
        p.error("Docker bind paths cannot contain commas")
    if output is not None and output.exists():
        p.error(
            "--output-dir must not already exist; each experiment owns a fresh cache"
        )
    docker = json.loads(command(["docker", "info", "--format", "{{json .}}"]))
    if docker.get("OSType") != "linux" or str(docker.get("CgroupVersion")) != "2":
        p.error("Docker must run Linux containers using cgroup v2")
    if action == "validate":
        print(
            json.dumps(
                {
                    "profile": profile,
                    "sources": sources,
                    "data_dir": str(data),
                    "docker": docker,
                },
                indent=2,
            )
        )
        return 0
    output.mkdir(parents=True)
    spec = vars(args).copy()
    spec.update(
        schema_version=2,
        abench_version=__version__,
        profile=profile,
        profile_name=profile["name"],
        sources=sources,
        label=args.label or output.name,
        processes=args.processes or 1,
        created_at=datetime.now(timezone.utc).isoformat(),
        model_commit=git_info(root, ["rev-parse", "HEAD"]),
        model_git_status=git_info(root, ["status", "--porcelain"]),
        docker={
            key: docker.get(key)
            for key in (
                "ServerVersion",
                "Architecture",
                "NCPU",
                "MemTotal",
                "KernelVersion",
                "CgroupVersion",
            )
        },
    )
    spec = json.loads(json.dumps(spec, default=str))
    (output / "model").mkdir()
    for config in profile["snapshot"]:
        source, target = root / config, output / "model" / config
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        else:
            shutil.copy2(source, target)
    for i, path in enumerate(overlays):
        shutil.copytree(path, output / "model" / f"overlay-{i}")
    shutil.copytree(
        PACKAGE / "runtime",
        output / "runner",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copytree(
        PACKAGE, output / "harness", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy2(PACKAGE / "Dockerfile", output / "production-benchmark.Dockerfile")
    spec["config_sha256"] = {
        str(path.relative_to(output / "model")): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted((output / "model").rglob("*"))
        if path.is_file()
    }
    spec["harness_sha256"] = {
        str(path.relative_to(output / "harness")): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted((output / "harness").rglob("*"))
        if path.is_file()
    }
    spec["input_files"] = {
        str(path.relative_to(data)): {
            "bytes": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in sorted(data.rglob("*"))
        if path.is_file()
    }
    write_json(output / "experiment.json", spec)
    image = "abench:" + uuid.uuid4().hex[:12]
    stage = "build"
    try:
        print(f"Building pinned packages; log: {output / 'build.log'}", flush=True)
        with tempfile.TemporaryDirectory() as context:
            shutil.copy2(
                output / "production-benchmark.Dockerfile", Path(context) / "Dockerfile"
            )
            shutil.copy2(
                PACKAGE / "runtime" / "build_sources.py",
                Path(context) / "build_sources.py",
            )
            write_json(
                Path(context) / "dependencies.json",
                {
                    "sources": sources,
                    "requirements": profile.get("requirements", []),
                    "constraints": profile.get("constraints", []),
                },
            )
            build = [
                "docker",
                "build",
                "-t",
                image,
                "--build-arg",
                f"PYTHON_IMAGE={profile.get('python_image', 'python:3.11-slim-bookworm')}",
            ]
            if args.platform:
                build += ["--platform", args.platform]
            command(build + [context], output / "build.log")
        spec["image_id"] = command(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"]
        )
        write_json(output / "experiment.json", spec)
        freeze = command(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "cat",
                image,
                "/opt/pip-freeze.txt",
            ]
        )
        (output / "pip-freeze.txt").write_text(freeze + "\n")
        provenance = command(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "cat",
                image,
                "/opt/source-provenance.json",
            ]
        )
        (output / "source-provenance.json").write_text(provenance + "\n")
        if seed:
            if (seed / "pip-freeze.txt").read_text().strip() != freeze.strip():
                raise ValueError("Cannot seed cache: installed dependencies differ")
            # Only flow artifacts are reused. Full warmup still checks coverage
            # under the new configuration before compilation-blocked measurement.
            shutil.copytree(seed / "cache/flows", output / "cache/flows")
        if args.sharrow:
            stage = "warmup"
            print("Preparing Sharrow cache with a complete matching run…", flush=True)
            container_phase(spec, output, data, image, "warmup")
        stage = "measured"
        print("Running measured model…", flush=True)
        container_phase(spec, output, data, image, "measured")
    except (Exception, KeyboardInterrupt) as error:
        spec["failure"] = {"phase": stage, "error": str(error)}
        write_json(output / "experiment.json", spec)
        raise
    finally:
        report(comparisons + [output], output / "report.html")
        print(f"Report: {output / 'report.html'}", flush=True)
    return 0 if load_run(output)["valid"] else 1


def entrypoint():
    """Expose CLI errors without an unnecessary Python traceback."""
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"Benchmark failed: {error}", file=sys.stderr)
        sys.exit(1)

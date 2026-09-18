"""Linux container supervisor; the model runs in a separate process tree."""

import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# multiprocessing's spawn imports this file again as __mp_main__.
if os.environ.get("BENCH_MODEL") == "1":
    from instrumentation import install

    install()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def phase_spec(spec, phase_name, data_root=Path("/data")):
    """Shrink only warmup; retain target config selection and the measured spec."""
    result = dict(spec)
    if phase_name != "warmup":
        return result
    cap = spec.get("warmup_households", 5000)
    target = spec["households"]
    if target == 0:
        # Zero denotes the full population, not an empty sample. Count only as
        # far as the cap for CSV, or use Parquet metadata without loading rows.
        profile = spec["profile"]
        filename = profile.get("household_table")
        if filename is None:
            stem = (
                profile.get("input_tables", {})
                .get("households", {})
                .get("file", "households")
            )
            filename = stem + ".csv"
        path = data_root / filename
        if not path.exists() and path.suffix == ".csv":
            path = path.with_suffix(".parquet")
        if path.suffix == ".csv" and path.is_file():
            with path.open(newline="") as stream:
                reader = csv.reader(stream)
                next(reader, None)
                target = 0
                for row in reader:
                    if row:
                        target += 1
                        if target >= cap:
                            break
        elif path.suffix == ".parquet" and path.is_file():
            import pyarrow.parquet as pq

            target = pq.ParquetFile(path).metadata.num_rows
        else:
            raise ValueError(
                "Cannot determine full-population warmup size; configure household_table or set a positive --households target"
            )
        if target < 1:
            raise ValueError("Cannot build a flow cache from an empty household table")
    result.update(
        households=min(target, cap),
        multiprocess=False,
        processes=1,
        _target_multiprocess=spec["multiprocess"],
    )
    return result


def make_state(
    spec,
    phase,
    model_root=Path("/model"),
    data_root=Path("/data"),
    results_root=Path("/results"),
):
    """Resolve model configs < profile defaults < overlays < explicit CLI controls.

    A generated inheriting config carries defaults through worker reconstruction;
    direct settings overrides are reserved for the experiment's required controls.
    """
    import activitysim.abm  # noqa: F401 -- register standard components
    import yaml
    from activitysim.core.workflow import State

    profile = spec["profile"]
    config_multiprocess = spec.get("_target_multiprocess", spec["multiprocess"])
    defaults = dict(profile.get("settings", {}))
    if profile.get("models_from"):
        base = yaml.safe_load((model_root / profile["models_from"]).read_text())
        defaults["models"] = [
            name
            for name in base["models"]
            if name not in profile.get("exclude_models", [])
        ]
    if config_multiprocess and profile.get("mp_settings"):
        mp = yaml.safe_load((model_root / profile["mp_settings"]).read_text())
        defaults["multiprocess_steps"] = mp["multiprocess_steps"]
    generated = phase / "profile-config"
    generated.mkdir(exist_ok=True)
    for name in ("settings.yaml", "settings_mp.yaml", "settings_mp_sharrow.yaml"):
        (generated / name).write_text(
            yaml.safe_dump(dict(defaults, inherit_settings=True))
        )
    configs = [
        model_root / f"overlay-{i}" for i in range(len(spec.get("config_overlay", [])))
    ]
    configs += [generated]
    if config_multiprocess:
        configs += [model_root / name for name in profile.get("mp_configs", [])]
    configs += [model_root / name for name in profile["configs"]]
    state = State.make_default(
        working_dir=model_root,
        configs_dir=configs,
        data_dir=data_root,
        output_dir=phase / "output",
        cache_dir=results_root / "cache/model",
        settings=dict(
            households_sample_size=spec["households"],
            multiprocess=spec["multiprocess"],
            num_processes=spec["processes"],
            sharrow="require" if spec["sharrow"] else False,
            fail_fast=True,
        ),
    )
    # Every sliced phase honors the requested count, including phases that have
    # their own worker count in production configs or explicit chunk overlays.
    for step in state.settings.multiprocess_steps or []:
        if isinstance(step, dict):
            if "slice" in step:
                step["num_processes"] = spec["processes"]
        elif getattr(step, "slice", None) is not None:
            step.num_processes = spec["processes"]
    state.filesystem.sharrow_cache_dir = results_root / "cache/flows"
    state.settings.sharrow_cache_dir = str(state.filesystem.sharrow_cache_dir)
    sys.path.insert(0, str(model_root))
    state.set("imported_extensions", [])
    for extension in profile.get("extensions", []):
        state.import_extensions(extension)
    if profile.get("adapter"):
        import importlib

        module, function = profile["adapter"].split(":")
        getattr(importlib.import_module(module), function)(state, spec, phase)
    state.set("run_timestamp", "benchmark")
    state.set("run_id", str(state.tracing.run_id))
    return state


def run_model(spec, phase):
    """Warm flows with a small serial run; measure the untouched target settings."""
    spec = phase_spec(spec, os.environ.get("BENCH_PHASE_NAME", phase.name))
    write_json(
        phase / "phase-settings.json",
        {key: spec[key] for key in ("households", "multiprocess", "processes")},
    )
    state = make_state(spec, phase)
    state.logging.config_logger()
    write_json(
        phase / "effective-settings.json", state.settings.model_dump(mode="json")
    )
    state.run.all(resume_after=None)
    if not spec["multiprocess"]:
        state.checkpoint.close_store()


def table_summary(directory, prefix="", tables=None):
    """Stream input/output tables so full-population summaries need bounded RAM."""
    import pandas as pd
    import pyarrow.parquet as pq

    result = {}
    tables = tables or {
        name: {}
        for name in (
            "households",
            "persons",
            "land_use",
            "tours",
            "trips",
            "joint_tour_participants",
            "vehicles",
        )
    }
    for name, options in tables.items():
        path = directory / f"{prefix}{options.get('file', name)}.csv"
        if path.exists():
            chunks = pd.read_csv(path, chunksize=100_000)
        else:
            path = directory / f"{prefix}{options.get('file', name)}.parquet"
            if not path.exists():
                continue
            chunks = (
                batch.to_pandas() for batch in pq.ParquetFile(path).iter_batches()
            )
        info = {"rows": 0, "totals": {}, "categories": {}}
        zones = set()
        for chunk in chunks:
            if name == "land_use":
                for col in options.get("zone_columns", ["taz", "TAZ"]):
                    if col in chunk:
                        zones.update(chunk[col].dropna().unique().tolist())
            info["rows"] += len(chunk)
            for col in options.get(
                "totals", ["TOTPOP", "TOTHH", "TOTEMP", "pop", "hh", "emp_total"]
            ):
                if col in chunk:
                    info["totals"][col] = info["totals"].get(col, 0) + float(
                        chunk[col].sum()
                    )
            for col in options.get(
                "categories",
                (
                    "tour_category",
                    "tour_type",
                    "tour_mode",
                    "trip_mode",
                    "primary_purpose",
                ),
            ):
                if col in chunk:
                    counts = info["categories"].setdefault(col, {})
                    for value, count in chunk[col].value_counts(dropna=False).items():
                        counts[str(value)] = counts.get(str(value), 0) + int(count)
        if zones:
            info["taz_count"] = len(zones)
        result[name] = info
    return result


def sample_memory(root, elapsed):
    """cgroup v2 charges shared pages once; file includes shmem, not vice versa."""
    stat = dict(
        line.split() for line in (root / "memory.stat").read_text().splitlines()
    )
    return {
        "elapsed_seconds": elapsed,
        "current_bytes": int((root / "memory.current").read_text()),
        "peak_bytes": int((root / "memory.peak").read_text()),
        "swap_bytes": int((root / "memory.swap.current").read_text()),
        "anonymous_bytes": int(stat.get("anon", 0)),
        "file_bytes": int(stat.get("file", 0)),
        "shared_bytes": int(stat.get("shmem", 0)),
    }


def supervise(spec, phase):
    """Measure only the model subprocess lifetime; summarize after sampling ends."""
    root = Path("/sys/fs/cgroup")
    if not (root / "memory.peak").exists():
        raise RuntimeError("A private cgroup v2 with memory.peak is required")
    (phase / "output").mkdir()
    for name in ("flows", "model"):
        Path(f"/results/cache/{name}").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, BENCH_MODEL="1", BENCH_PHASE_DIR=str(phase))
    env["BENCH_STRICT_CACHE"] = "0"
    env["BENCH_TRACK_CACHE"] = (
        "1"
        if spec["sharrow"]
        and os.environ.get("BENCH_PHASE_NAME", phase.name) == "measured"
        else "0"
    )
    started = time.perf_counter()
    env["BENCH_STARTED_MONOTONIC"] = str(started)
    with (phase / "memory.csv").open("w", buffering=1) as stream:
        writer = csv.DictWriter(stream, fieldnames=sample_memory(root, 0).keys())
        writer.writeheader()
        writer.writerow(sample_memory(root, 0))
        process = subprocess.Popen(
            [sys.executable, __file__, "model", str(phase)], env=env
        )
        while True:
            writer.writerow(sample_memory(root, time.perf_counter() - started))
            if process.poll() is not None:
                break
            try:
                process.wait(timeout=spec["interval"])
            except subprocess.TimeoutExpired:
                pass
    write_json(
        phase / "status.json",
        {
            "returncode": process.returncode,
            "elapsed_seconds": time.perf_counter() - started,
        },
    )
    # This separate process keeps pandas/Arrow and summary allocations out of the
    # measured lifetime; the host report uses only the recorded cgroup samples.
    subprocess.run([sys.executable, __file__, "summary", str(phase)], check=True)
    return process.returncode


if __name__ == "__main__":
    mode, phase_arg = sys.argv[1:]
    phase = Path(phase_arg)
    spec = json.loads(
        Path(os.environ.get("BENCH_SPEC_PATH", "/results/experiment.json")).read_text()
    )
    if mode == "model":
        run_model(spec, phase)
    elif mode == "summary":
        write_json(
            phase / "input-summary.json",
            table_summary(Path("/data"), tables=spec["profile"].get("input_tables")),
        )
        write_json(
            phase / "output-summary.json",
            table_summary(
                phase / "output",
                spec["profile"].get("output_prefix", "final_"),
                spec["profile"].get("output_tables"),
            ),
        )
    else:
        sys.exit(supervise(spec, phase))

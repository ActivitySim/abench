"""Retry completed runs that prepared new flow signatures, never model errors."""

import shutil

from .common import write_json
from .failures import BenchmarkFailure, describe_failure
from .report import load_run


def measured_attempts(spec, output, run_phase, publish_cache):
    """Select only a compilation-free attempt for the report.

    Each attempt starts in a new container with fresh outputs and model caches.
    Only compiled flows survive; failed preparation timings never enter reports.
    """
    spec["attempts"] = []
    for number in range(1, spec.get("cache_retries", 2) + 2):
        model_cache = output / "cache/model"
        if model_cache.exists():
            shutil.rmtree(model_cache)
        staging = output / "attempts" / f"attempt-{number:03d}"
        relative = str(staging.relative_to(output))
        record = {"number": number, "directory": relative, "status": "running"}
        spec["attempts"].append(record)
        spec["measured_directory"] = relative
        write_json(output / "experiment.json", spec)
        print(f"Running measured attempt {number}…", flush=True)
        try:
            run_phase(relative)
            # Check all ordinary validity conditions before deciding to retry.
            if not load_run(output, allow_cache_misses=True)["valid"]:
                raise BenchmarkFailure(
                    describe_failure(output, "measured", ignore_cache=True)
                )
        except BaseException:
            record["status"] = "failed"
            write_json(output / "experiment.json", spec)
            raise
        phase = staging
        misses = sum(
            len(path.read_text().splitlines())
            for path in phase.glob("cache-miss-*.txt")
        )
        record.update(
            compilations=misses,
            status="cache preparation" if misses else "accepted",
        )
        publish_cache()
        write_json(output / "experiment.json", spec)
        if not misses:
            # Convenience alias only after containers finish; attempt directories
            # never move or change identity while Docker may cache their paths.
            (output / "measured").symlink_to(relative, target_is_directory=True)
            return
        if number > spec.get("cache_retries", 2):
            raise BenchmarkFailure(
                f"Flow compilation persisted after {number} completed attempts. "
                "No valid benchmark was produced. Inspect attempts/*/cache-miss-details-*.jsonl "
                "for changing signatures; increase --cache-retries if appropriate. "
                f"Diagnostics: {output}"
            )
        print(
            f"Attempt {number} compiled {misses} flow signatures; retained as cache "
            f"preparation at {staging}. Retrying with a fresh model state…",
            flush=True,
        )

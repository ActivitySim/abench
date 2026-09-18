"""Translate container exit codes into useful model failure messages."""

import json
import re
from pathlib import Path

from .common import read_json


class BenchmarkFailure(ValueError):
    """An experiment failed with diagnostics retained beside its report."""


def tail(path, limit=131072):
    """Read a bounded log tail even when a full model log is many gigabytes."""
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - limit))
        return stream.read().decode("utf-8", errors="replace")


def describe_failure(output, phase, *, ignore_cache=False):
    """Prefer explicit cache/OOM evidence over a generic worker exit exception."""
    output = Path(output)
    spec = read_json(output / "experiment.json", {})
    directory = output / (
        spec.get("measured_directory", "measured") if phase == "measured" else phase
    )
    log = output / "build.log" if phase == "build" else directory / "console.log"
    docker = read_json(directory / "docker-state.json", {})
    components = set()
    for path in directory.glob("components-*.jsonl"):
        for line in tail(path).splitlines():
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not row.get("succeeded", True):
                components.add(row["component"])
    where = f" in {', '.join(sorted(components))}" if components else ""
    misses = sorted(
        {
            line.strip()
            for path in directory.glob("cache-miss-*.txt")
            for line in tail(path).splitlines()
            if line.strip()
        }
    )
    spec = read_json(output / "experiment.json", {})
    retry_mode = "cache_retries" in spec
    completed = (
        docker.get("ExitCode") == 0
        and read_json(directory / "status.json", {}).get("returncode") == 0
    )
    if (
        misses
        and not ignore_cache
        and retry_mode
        and completed
        and not docker.get("OOMKilled")
    ):
        reason = "Completed attempt compiled Sharrow flows; its runtime and memory are excluded."
        remedy = (
            spec.get("failure", {}).get("error")
            or "See attempt history and cache-miss-details-*.jsonl for required signatures."
        )
    elif misses and not ignore_cache and not retry_mode:
        reason = f"Sharrow flow cache miss{where}: " + ", ".join(
            Path(name).parent.name for name in misses
        )
        remedy = "Results rejected; no flow compilation was allowed. The warmup did not cover the required flow/type signature. Increase warmup_households (up to the target sample) and start a new experiment."
    elif docker.get("OOMKilled"):
        reason = f"Docker killed the container for exceeding its memory limit{where}."
        remedy = (
            "Increase the container/VM memory budget or reduce component chunk sizes."
        )
    elif docker.get("ExitCode") == 0 and phase == "measured":
        spec = read_json(output / "experiment.json", {})
        summaries = read_json(directory / "output-summary.json", {})
        actual = summaries.get("households", {}).get("rows")
        requested = spec.get("households", 0)
        if requested and actual != requested:
            reason = f"Household sample mismatch: requested {requested}, output contains {actual if actual is not None else 'no household table'}."
        else:
            reason = "Required runtime or memory measurements are missing."
        remedy = "Results rejected even though the container exited successfully."
    else:
        errors = re.findall(
            r"^([\w.]+(?:Error|Exception): .+)$", tail(log), re.MULTILINE
        )
        useful = [e for e in errors if "SubprocessError: Process " not in e]
        reason = (
            useful
            or errors
            or [
                docker.get("Error")
                or f"Container exited with code {docker.get('ExitCode', 'unavailable')}"
            ]
        )[0]
        remedy = "See the retained log for the complete traceback."
    return f"{output.name}: {phase} failed{where if not misses else ''}. {reason}\n{remedy}\nLog: {log}\nReport: {output / 'report.html'}"

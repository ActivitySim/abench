"""Offline readers and interactive reports, including legacy experiment support."""

import csv
import html
import json
import math
import re
import statistics

from .common import read_json, write_json
from .failures import describe_failure

COLORS = ("#0072b2", "#d55e00", "#009e73", "#cc79a7", "#e69f00", "#56b4e9")


def clock_seconds(value):
    """Parse elapsed MM:SS or HH:MM:SS values from ActivitySim logs."""
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def legacy_windows(phase):
    """Recover approximate windows for artifacts predating monotonic timestamps.

    MP completion messages arrive at the parent after execution and can include
    checkpoint time. Their relative logging clock also starts slightly after the
    sampler. Keep this fallback explicitly approximate instead of inventing exact
    timestamps by summing worker durations across unmeasured checkpoint gaps.
    """
    path = phase / "console.log"
    windows = {}
    if not path.exists():
        return windows
    pattern = re.compile(
        r"^\[(?P<end>[\d:.]+)\].*?\b(?P<process>mp_\w+) "
        r"(?P<component>\w+) : (?P<duration>[\d.]+) seconds\b"
    )
    serial = re.compile(
        r"^\[(?P<end>[\d:.]+)\].*?time to execute run\."
        r"(?P<component>\w+) : (?P<duration>[\d:.]+)(?: seconds)?\s*$"
    )
    for line in path.read_text(errors="replace").splitlines():
        match = pattern.search(line) or serial.search(line)
        if match:
            end = clock_seconds(match["end"])
            duration = clock_seconds(match["duration"])
            process = match.groupdict().get("process") or "MainProcess"
            key = (process, match["component"])
            windows.setdefault(key, []).append(
                {
                    "start_seconds": max(0.0, end - duration),
                    "end_seconds": end,
                    "source": "approximate completion log",
                }
            )
    return windows


def component_windows(observations, phase):
    """Retain each worker interval separately, including overlaps and gaps."""
    fallback = (
        legacy_windows(phase)
        if any(
            "start_seconds" not in row or "end_seconds" not in row
            for row in observations
        )
        else {}
    )
    windows = []
    for row in observations:
        key = (row.get("process", "MainProcess"), row["component"])
        # Consume a matching fallback even for a timestamped observation so a
        # mixed-format artifact cannot assign it to a later repeated execution.
        matches = fallback.get(key, [])
        approximate = matches.pop(0) if matches else None
        if "start_seconds" in row and "end_seconds" in row:
            interval = {k: row[k] for k in ("start_seconds", "end_seconds")}
            interval["source"] = "recorded monotonic clock"
        elif approximate:
            interval = approximate
        else:
            continue
        start, end = interval["start_seconds"], interval["end_seconds"]
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start <= end):
            continue
        windows.append(
            dict(
                interval,
                component=row["component"],
                process=key[0],
                pid=row.get("pid"),
                succeeded=row["succeeded"],
            )
        )
    return windows


def load_run(directory, *, allow_cache_misses=False):
    """Use raw worker observations, never duplicate ActivitySim's locutor CSV."""
    spec = read_json(directory / "experiment.json")
    if not spec or spec.get("schema_version") not in (1, 2):
        raise ValueError(f"Not a supported benchmark experiment: {directory}")
    phase = directory / spec.get("measured_directory", "measured")
    grouped = {}
    observations = []
    for path in sorted(phase.glob("components-*.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            observations.append(row)
            if row["succeeded"]:
                grouped.setdefault(row["component"], []).append(row["seconds"])
    components = {
        key: {
            "n": len(values),
            "mean": statistics.fmean(values),
            "sd": statistics.pstdev(values),
            "maximum": max(values),
        }
        for key, values in grouped.items()
    }
    memory = []
    if (phase / "memory.csv").exists():
        with (phase / "memory.csv").open() as stream:
            memory = [
                {key: float(value) for key, value in row.items()}
                for row in csv.DictReader(stream)
            ]
    status = read_json(phase / "status.json", {})
    docker = read_json(phase / "docker-state.json", {})
    outputs = read_json(phase / "output-summary.json", {})
    requested = spec.get("households", 0)
    sample_matches = (
        not requested or outputs.get("households", {}).get("rows") == requested
    )
    valid = (
        sample_matches
        and not spec.get("failure")
        and status.get("returncode") == 0
        and docker.get("ExitCode") == 0
        and bool(memory)
        and bool(components)
        and not docker.get("OOMKilled")
        and (allow_cache_misses or not list(phase.glob("cache-miss-*.txt")))
    )
    warmup_settings = read_json(directory / "warmup/phase-settings.json", {})
    effective = read_json(directory / "warmup/effective-settings.json", {})
    if effective:
        warmup_settings = {
            "households": effective.get("households_sample_size"),
            "multiprocess": effective.get("multiprocess"),
            "processes": effective.get("num_processes"),
        }
    return {
        "spec": spec,
        "components": components,
        "component_windows": component_windows(observations, phase),
        "memory": memory,
        "status": status,
        "valid": valid,
        "inputs": read_json(phase / "input-summary.json", {}),
        "outputs": outputs,
        "failure_reason": (
            spec.get("failure", {}).get("error")
            or describe_failure(
                directory, spec.get("failure", {}).get("phase", "measured")
            )
        )
        if not valid
        else None,
        "warmup_settings": warmup_settings,
        "peak": max((r["peak_bytes"] for r in memory), default=0),
        "docker": docker,
        "path": str(directory),
    }


def escape(value):
    return html.escape(str(value), quote=True)


def memory_chart(run, xmax, ymax):
    """Standalone SVG uses common axes across experiments for fair comparison."""
    points = " ".join(
        f"{55 + 620 * r['elapsed_seconds'] / xmax:.2f},{235 - 200 * r['current_bytes'] / ymax:.2f}"
        for r in run["memory"]
    )
    # Mapped skim pages can fill the cgroup with reclaimable file cache. Show
    # anonymous plus shared allocations separately without calling them total RAM.
    allocation_line = ""
    allocation_legend = ""
    if run["memory"] and all("anonymous_bytes" in row for row in run["memory"]):
        allocations = " ".join(
            f"{55 + 620 * row['elapsed_seconds'] / xmax:.2f},{235 - 200 * (row['anonymous_bytes'] + row.get('shared_bytes', 0)) / ymax:.2f}"
            for row in run["memory"]
        )
        allocation_line = f'<polyline fill="none" stroke="#009e73" stroke-width="2" stroke-dasharray="5 3" points="{allocations}"/>'
        allocation_legend = "<p>Blue: total container memory. Green dashed: anonymous + shared memory (excludes mapped skim pages and other file cache).</p>"
    ticks = "".join(
        f'<text x="48" y="{239 - 200 * i / 4}" text-anchor="end">{ymax * i / 4 / 2**30:.1f}</text>'
        f'<line x1="55" x2="675" y1="{235 - 200 * i / 4}" y2="{235 - 200 * i / 4}" stroke="#ddd"/>'
        f'<text x="{55 + 620 * i / 4}" y="255" text-anchor="middle">{xmax * i / 4:.0f}</text>'
        for i in range(5)
    )
    bands = []
    for window in run["component_windows"]:
        start, end = window["start_seconds"], window["end_seconds"]
        # Clip to the sampled chart domain; never bridge gaps between workers.
        left, right = min(start, xmax), min(end, xmax)
        title = (
            f"{window['process']}: {start:.3f}–{end:.3f} s "
            f"({window['source']}; {'completed' if window['succeeded'] else 'failed'})"
        )
        bands.append(
            f'<rect class="component-window" data-component="{escape(window["component"])}" '
            f'data-source="{escape(window["source"])}" style="display:none" '
            f'x="{55 + 620 * left / xmax:.3f}" y="35" width="{620 * (right - left) / xmax:.3f}" height="200" '
            f'fill="#e69f00" fill-opacity="0.17" stroke="#ac7100" stroke-opacity="0.35">'
            f"<title>{escape(title)}</title></rect>"
        )
    return (
        '<div class="memory-panel">'
        f'<svg viewBox="0 0 710 285" role="img" aria-label="Container memory by elapsed seconds">{ticks}{"".join(bands)}'
        f'<polyline fill="none" stroke="#0072b2" stroke-width="2" points="{points}"/>{allocation_line}'
        '<text x="55" y="20">GiB</text><text x="310" y="278">Elapsed seconds</text></svg>'
        f"{allocation_legend}"
        '<p class="window-status" aria-live="polite">Choose a component to highlight its worker windows.</p></div>'
    )


def runtime_chart(runs, components):
    """Grouped horizontal bars compare component means with population SD whiskers."""
    maximum = (
        max(
            (c["mean"] + c["sd"] for run in runs for c in run["components"].values()),
            default=1,
        )
        or 1
    )
    rows = []
    y = 30
    for name in components:
        rows.append(f'<text x="5" y="{y + 12}">{escape(name)}</text>')
        for i, run in enumerate(runs):
            value = run["components"].get(name)
            if value:
                scale = 530 / maximum
                width = value["mean"] * scale
                lo, hi = (
                    max(0, value["mean"] - value["sd"]) * scale,
                    (value["mean"] + value["sd"]) * scale,
                )
                rows.append(
                    f'<rect x="310" y="{y}" width="{width:.2f}" height="12" fill="{COLORS[i % len(COLORS)]}"><title>{escape(run["spec"]["label"])}: {value["mean"]:.3f} ± {value["sd"]:.3f} s</title></rect><path d="M {310 + lo:.2f} {y + 6} H {310 + hi:.2f}" stroke="#222"/>'
                )
            y += 17
        y += 10
    return f'<svg viewBox="0 0 900 {y + 20}" role="img" aria-label="Component runtime mean and standard deviation"><text x="310" y="18">0 seconds</text><text x="790" y="18">{maximum:.1f} s</text>{"".join(rows)}</svg>'


def experiment_card(run, xmax, ymax):
    """Present the primary settings and counts before detailed provenance."""
    spec = run["spec"]
    settings = "".join(
        f"<tr><th>{escape(key.replace('_', ' '))}</th><td>{escape(spec.get(key, 'unavailable'))}</td></tr>"
        for key in (
            "sources",
            "profile_name",
            "abench_version",
            "activitysim_commit",
            "sharrow_commit",
            "multiprocess",
            "processes",
            "sharrow",
            "households",
            "warmup_households",
            "cache_retries",
            "data_dir",
            "output_dir",
            "memory",
            "shm_size",
            "interval",
            "platform",
            "compare",
            "config_overlay",
            "cache_from",
            "reuse_flows",
            "flow_cache_dir",
            "flow_cache",
        )
    )
    counts = []
    for name in dict.fromkeys(
        [
            "households",
            "persons",
            "land_use",
            "tours",
            "trips",
            *run["inputs"],
            *run["outputs"],
        ]
    ):
        values = [run[key].get(name, {}).get("rows") for key in ("inputs", "outputs")]
        cells = "".join(
            f"<td>{value:,}</td>" if value is not None else "<td>—</td>"
            for value in values
        )
        counts.append(
            f"<tr><th>{escape(spec.get('profile', {}).get('zone_label', 'zones') if name == 'land_use' else name)}</th>{cells}</tr>"
        )
    taz_cells = "".join(
        f"<td>{run[key].get('land_use', {}).get('taz_count', '—')}</td>"
        for key in ("inputs", "outputs")
    )
    counts.append(f"<tr><th>TAZs</th>{taz_cells}</tr>")
    elapsed = run["status"].get("elapsed_seconds")
    elapsed = f"{elapsed:.3f}" if elapsed is not None else "unavailable"
    failure = (
        f"<pre>{escape(run['failure_reason'])}</pre>"
        if run.get("failure_reason")
        else ""
    )
    attempts = spec.get("attempts", [])
    attempt_table = ""
    if attempts:
        rows = "".join(
            "<tr>"
            + "".join(
                f"<td>{escape(attempt.get(key, '—'))}</td>"
                for key in ("number", "status", "compilations", "directory")
            )
            + "</tr>"
            for attempt in attempts
        )
        attempt_table = (
            "<h3>Attempt history</h3><p>Cache-preparation attempts are excluded from benchmark results.</p>"
            "<table><tr><th>Attempt</th><th>Result</th><th>Flow compilations</th><th>Artifacts</th></tr>"
            + rows
            + "</table>"
        )
    details = "".join(
        f"<details><summary>{title}</summary><pre>{escape(json.dumps(value, indent=2))}</pre></details>"
        for title, value in (
            ("Complete settings and provenance", spec),
            ("Actual cache-build settings", run.get("warmup_settings", {})),
            ("Input totals and categories", run["inputs"]),
            ("Output totals and categories", run["outputs"]),
            ("Container exit and OOM status", run["docker"]),
        )
    )
    return (
        f"<article><h2>{escape(spec['label'])}</h2><p><b>{'SUCCEEDED' if run['valid'] else 'FAILED / INCOMPLETE'}</b>"
        f" · elapsed: {elapsed} s · peak: {run['peak'] / 2**30:.3f} GiB</p>{failure}{attempt_table}"
        f"{memory_chart(run, xmax, ymax)}<h3>Experiment settings</h3><table>{settings}</table>"
        f"<h3>Population and outputs</h3><table><tr><th>Table</th><th>Input rows</th><th>Output rows</th></tr>{''.join(counts)}</table>"
        f"<p>Output households and persons are the realized sample.</p>{details}</article>"
    )


def comparison_notes(runs):
    """Surface workload/environment differences without claiming causal speedups."""
    notes = []
    fields = (
        "profile_name",
        "sources",
        "households",
        "multiprocess",
        "processes",
        "sharrow",
        "config_sha256",
        "input_files",
        "docker",
    )
    for field in fields:
        values = [json.dumps(run["spec"].get(field), sort_keys=True) for run in runs]
        if len(set(values)) > 1:
            notes.append(field.replace("_", " "))
    if not notes:
        return ""
    return (
        "<p><b>Comparison differences:</b> "
        + escape(", ".join(notes))
        + ". Inspect provenance before attributing differences to a source revision.</p>"
    )


def report(directories, destination):
    """Generate a portable, offline HTML report and normalized comparison data."""
    runs = [load_run(path) for path in directories]
    components = list(dict.fromkeys(name for run in runs for name in run["components"]))
    headers = "".join(f"<th>{escape(r['spec']['label'])}</th>" for r in runs)
    table = []
    for name in components:
        eligible = [
            r["components"][name]["mean"]
            for r in runs
            if r["valid"] and name in r["components"]
        ]
        fastest = min(eligible, default=None)
        cells = []
        for run in runs:
            c = run["components"].get(name)
            if c is None:
                cells.append("<td>—</td>")
                continue
            winner = (
                run["valid"]
                and fastest is not None
                and math.isclose(c["mean"], fastest, rel_tol=1e-9)
            )
            cells.append(
                f'<td class="{"fastest" if winner else ""}">{c["mean"]:.3f} ± {c["sd"]:.3f} s<br><small>n={c["n"]}; max={c["maximum"]:.3f} s</small></td>'
            )
        table.append(f"<tr><th>{escape(name)}</th>{''.join(cells)}</tr>")
    xmax = (
        max((row["elapsed_seconds"] for r in runs for row in r["memory"]), default=1)
        or 1
    )
    ymax = (
        max((row["current_bytes"] for r in runs for row in r["memory"]), default=1) or 1
    )
    cards = [experiment_card(run, xmax, ymax) for run in runs]
    legend = " ".join(
        f'<span style="color:{COLORS[i % len(COLORS)]}">■ {escape(r["spec"]["label"])}</span>'
        for i, r in enumerate(runs)
    )
    selectable = list(
        dict.fromkeys(
            components
            + [
                window["component"]
                for run in runs
                for window in run["component_windows"]
            ]
        )
    )
    options = "".join(
        f'<option value="{escape(name)}">{escape(name)}</option>' for name in selectable
    )
    # Component names live only in escaped HTML attributes/text, never in JS.
    selector_script = """
<script>
const selector = document.getElementById('memory-component');
function highlightComponent() {
  document.querySelectorAll('.memory-panel').forEach(panel => {
    let count = 0;
    let approximate = false;
    panel.querySelectorAll('.component-window').forEach(band => {
      const selected = selector.value !== '' && band.dataset.component === selector.value;
      band.style.display = selected ? '' : 'none';
      if (selected) {
        count += 1;
        approximate ||= band.dataset.source.startsWith('approximate');
      }
    });
    const status = panel.querySelector('.window-status');
    status.textContent = selector.value === ''
      ? 'Choose a component to highlight its worker windows.'
      : count === 0
        ? 'No execution-window data available for this component in this experiment.'
        : `${count} worker execution window${count === 1 ? '' : 's'} highlighted. ` +
          (approximate ? 'Approximate timing reconstructed from completion logs.' :
                         'Recorded on the memory sampler’s clock.');
  });
}
selector.addEventListener('change', highlightComponent);
highlightComponent();
</script>
"""
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>abench report</title>
<style>body{{font:15px system-ui;margin:2rem;color:#17212b}}.cards{{display:flex;gap:24px;overflow-x:auto}}article{{flex:1;min-width:420px;border:1px solid #ccc;padding:16px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}svg{{width:100%;min-width:420px}}table{{border-collapse:collapse;width:100%}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #ddd}}.fastest{{background:#d7f3dc}}small{{color:#58616a}}.runtime{{max-width:1200px}}.scroll{{overflow-x:auto}}</style>
<h1>abench report</h1><p>Whole-container cgroup v2 memory counts shared pages once, including file cache, kernel memory and the supervisor. Swap is recorded separately in memory.csv. Cache preparation and post-run summaries are excluded. Peak is the kernel high-water mark sampled during the model lifetime, including container startup. Memory panels use identical axes.</p>
{comparison_notes(runs)}
<label for="memory-component"><b>Highlight component:</b></label>
<select id="memory-component"><option value="">None</option>{options}</select>
<p>Selection applies to every memory chart. Each translucent band is one worker execution; darker overlaps indicate concurrent workers. Gaps remain unshaded. Hover over a band for its worker and time range.</p>
<noscript>Enable JavaScript to select and highlight component windows.</noscript>
<div class="cards">{"".join(cards)}</div><h2>Component runtimes</h2><p>Mean ± population standard deviation across worker executions, with observation count and maximum. These describe worker imbalance, not uncertainty across repeated experiments. Component timings exclude pipeline checkpoint writes; elapsed time includes startup, I/O and coordination. Parallel component times must not be summed to estimate wall time. Green cells mark the fastest successful experiment's mean; failed runs are excluded from winners.</p><div class="scroll"><table><tr><th>Component</th>{headers}</tr>{"".join(table)}</table></div><h2>Runtime comparison</h2><p>{legend}</p><div class="runtime">{runtime_chart(runs, components)}</div>{selector_script}</html>"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document)
    write_json(destination.with_suffix(".json"), runs)

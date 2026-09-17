"""Offline readers and interactive reports, including legacy experiment support."""

import csv
import html
import json
import math
import re
import statistics

from .common import read_json, write_json

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


def load_run(directory):
    """Use raw worker observations, never duplicate ActivitySim's locutor CSV."""
    spec = read_json(directory / "experiment.json")
    if not spec or spec.get("schema_version") not in (1, 2):
        raise ValueError(f"Not a supported benchmark experiment: {directory}")
    phase = directory / "measured"
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
    component_summaries = read_json(phase / "component-summary.json", {})
    requested = spec.get("households", 0)
    sample_matches = (
        not requested or outputs.get("households", {}).get("rows") == requested
    )
    valid = (
        sample_matches
        and status.get("returncode") == 0
        and docker.get("ExitCode") == 0
        and bool(memory)
        and bool(components)
        and not docker.get("OOMKilled")
        and not list(phase.glob("cache-miss-*.txt"))
    )
    return {
        "spec": spec,
        "components": components,
        "component_windows": component_windows(observations, phase),
        "memory": memory,
        "status": status,
        "valid": valid,
        "inputs": read_json(phase / "input-summary.json", {}),
        "outputs": outputs,
        "component_summaries": component_summaries,
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


def outcome_keys(runs):
    """Preserve first-appearance order across runs, mirroring component ordering."""
    keys = []
    for run in runs:
        for component, cdata in run["component_summaries"].get("components", {}).items():
            for table, tdata in cdata.get("tables", {}).items():
                for outcome in tdata.get("outcomes", {}):
                    key = (component, table, outcome)
                    if key not in keys:
                        keys.append(key)
    return keys


def outcome_data(run, key):
    component, table, outcome = key
    tables = (
        run["component_summaries"].get("components", {}).get(component, {}).get("tables", {})
    )
    return tables.get(table, {}).get("outcomes", {}).get(outcome)


def outcome_categories(runs, key):
    """Union category labels across runs; nulls sort last for readability."""
    categories = []
    for run in runs:
        for category in (outcome_data(run, key) or {}).get("counts", {}):
            if category not in categories:
                categories.append(category)
    return sorted(categories, key=lambda c: (c == "<null>", c))


def outcome_totals(runs, key):
    return [sum((outcome_data(run, key) or {}).get("counts", {}).values()) for run in runs]


def outcome_table(runs, key, categories):
    headers = "".join(f"<th>{escape(run['spec']['label'])}</th>" for run in runs)
    totals = outcome_totals(runs, key)
    rows = []
    for category in categories:
        cells = []
        for run, total in zip(runs, totals):
            count = (outcome_data(run, key) or {}).get("counts", {}).get(category)
            if count is None:
                cells.append("<td>—</td>")
            else:
                share = f"{count / total * 100:.1f}%" if total else "—"
                cells.append(f"<td>{count:,}<br><small>{share}</small></td>")
        rows.append(f"<tr><th>{escape(category)}</th>{''.join(cells)}</tr>")
    omitted = [
        run["spec"]["label"]
        for run in runs
        if "counts_omitted" in (outcome_data(run, key) or {})
    ]
    note = (
        f"<p><small>Exact category counts omitted (category limit exceeded) for: "
        f"{escape(', '.join(omitted))}.</small></p>"
        if omitted
        else ""
    )
    return f"<table><tr><th>Value</th>{headers}</tr>{''.join(rows)}</table>{note}"


def outcome_chart(runs, key, categories):
    """Grouped horizontal bars compare each category's share of non-null observations."""
    totals = outcome_totals(runs, key)
    rows = []
    y = 30
    for category in categories:
        rows.append(f'<text x="5" y="{y + 12}">{escape(category)}</text>')
        for i, (run, total) in enumerate(zip(runs, totals)):
            count = (outcome_data(run, key) or {}).get("counts", {}).get(category)
            if count and total:
                share = count / total * 100
                width = share * 5.3
                rows.append(
                    f'<rect x="310" y="{y}" width="{width:.2f}" height="12" fill="{COLORS[i % len(COLORS)]}">'
                    f'<title>{escape(run["spec"]["label"])}: {count:,} ({share:.1f}%)</title></rect>'
                )
            y += 17
        y += 10
    return (
        f'<svg viewBox="0 0 900 {y + 20}" role="img" aria-label="Outcome category share">'
        f'<text x="310" y="18">0%</text><text x="790" y="18">100%</text>{"".join(rows)}</svg>'
    )


def numeric_table(runs, key):
    headers = "".join(f"<th>{escape(run['spec']['label'])}</th>" for run in runs)
    rows = []
    for label, stat in (("Mean", "mean"), ("Minimum", "min"), ("Maximum", "max")):
        cells = []
        for run in runs:
            numeric = (outcome_data(run, key) or {}).get("numeric")
            cells.append(f"<td>{numeric[stat]:.3f}</td>" if numeric else "<td>—</td>")
        rows.append(f"<tr><th>{label}</th>{''.join(cells)}</tr>")
    return f"<table><tr><th>Statistic</th>{headers}</tr>{''.join(rows)}</table>"


def numeric_chart(runs, key):
    """Grouped bars compare the mean with a min/max whisker on a shared scale."""
    values = [(outcome_data(run, key) or {}).get("numeric") for run in runs]
    present = [v for v in values if v]
    minimum = min((v["min"] for v in present), default=0)
    maximum = max((v["max"] for v in present), default=1)
    span = (maximum - minimum) or 1
    rows = []
    y = 30
    for i, (run, value) in enumerate(zip(runs, values)):
        if value:
            scale = 530 / span
            mean_x = 310 + (value["mean"] - minimum) * scale
            lo_x = 310 + (value["min"] - minimum) * scale
            hi_x = 310 + (value["max"] - minimum) * scale
            rows.append(
                f'<rect x="{mean_x - 2:.2f}" y="{y}" width="4" height="12" fill="{COLORS[i % len(COLORS)]}">'
                f'<title>{escape(run["spec"]["label"])}: mean {value["mean"]:.3f}, '
                f'range [{value["min"]:.3f}, {value["max"]:.3f}]</title></rect>'
                f'<path d="M {lo_x:.2f} {y + 6} H {hi_x:.2f}" stroke="#222"/>'
            )
        y += 17
    return (
        f'<svg viewBox="0 0 900 {y + 20}" role="img" aria-label="Outcome numeric range">'
        f'<text x="310" y="18">{minimum:.2f}</text><text x="790" y="18">{maximum:.2f}</text>'
        f'{"".join(rows)}</svg>'
    )


def summary_table(runs, key):
    """High-cardinality, non-numeric outcomes (e.g. free-form ids) get counts only."""
    headers = "".join(f"<th>{escape(run['spec']['label'])}</th>" for run in runs)
    rows = []
    for label, stat in (
        ("Non-null count", "count"),
        ("Nulls", "nulls"),
        ("Distinct values", "distinct"),
    ):
        cells = []
        for run in runs:
            data = outcome_data(run, key)
            cells.append(f"<td>{data[stat]:,}</td>" if data and stat in data else "<td>—</td>")
        rows.append(f"<tr><th>{label}</th>{''.join(cells)}</tr>")
    return f"<table><tr><th>Statistic</th>{headers}</tr>{''.join(rows)}</table>"


def outcomes_section(runs):
    """Compare per-model outcome distributions across runs, switching table/plot."""
    keys = outcome_keys(runs)
    if not keys:
        return ""
    options = []
    panels = []
    for index, key in enumerate(keys):
        component, table, outcome = key
        anchor = f"outcome-{index}"
        options.append(
            f'<option value="{anchor}">{escape(f"{component} · {table} · {outcome}")}</option>'
        )
        categories = outcome_categories(runs, key)
        if categories:
            body = (
                f'<div class="table-view">{outcome_table(runs, key, categories)}</div>'
                f'<div class="plot-view">{outcome_chart(runs, key, categories)}</div>'
            )
        elif any((outcome_data(run, key) or {}).get("numeric") for run in runs):
            body = (
                f'<div class="table-view">{numeric_table(runs, key)}</div>'
                f'<div class="plot-view">{numeric_chart(runs, key)}</div>'
            )
        else:
            body = summary_table(runs, key)
        style = "" if index == 0 else "display:none"
        panels.append(f'<div class="outcome-panel" id="{anchor}" style="{style}">{body}</div>')
    return (
        "<h2>Component outcomes</h2>"
        "<p>Model choice distributions recovered from ActivitySim checkpoints, merged across "
        "worker partitions. Shares are of non-null observations within each run; household "
        "samples can differ across experiments, so compare shares rather than raw counts. "
        "Outcomes too high-cardinality for exact category counts show mean with a min/max "
        "range instead.</p>"
        '<label for="outcome-select"><b>Outcome:</b></label> '
        f'<select id="outcome-select">{"".join(options)}</select> '
        '<span class="outcome-toggle">'
        '<button type="button" data-view="table" class="active">Table</button>'
        '<button type="button" data-view="plot">Plot</button>'
        "</span>"
        f'<div id="outcomes-section" class="scroll">{"".join(panels)}</div>'
    )


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
            "use_explicit_error_terms",
            "households",
            "data_dir",
            "output_dir",
            "memory",
            "shm_size",
            "interval",
            "platform",
            "compare",
            "config_overlay",
            "cache_from",
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
    failure = f"<p>{escape(spec['failure'])}</p>" if spec.get("failure") else ""
    details = "".join(
        f"<details><summary>{title}</summary><pre>{escape(json.dumps(value, indent=2))}</pre></details>"
        for title, value in (
            ("Complete settings and provenance", spec),
            ("Input totals and categories", run["inputs"]),
            ("Output totals and categories", run["outputs"]),
            ("Component outcome summaries", run["component_summaries"]),
            ("Container exit and OOM status", run["docker"]),
        )
    )
    return (
        f"<article><h2>{escape(spec['label'])}</h2><p><b>{'SUCCEEDED' if run['valid'] else 'FAILED / INCOMPLETE'}</b>"
        f" · elapsed: {elapsed} s · peak: {run['peak'] / 2**30:.3f} GiB</p>{failure}"
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
    outcomes_html = outcomes_section(runs)
    # Outcome labels live only in escaped HTML attributes/text, never in JS.
    outcomes_script = """
<script>
const outcomeSelect = document.getElementById('outcome-select');
const outcomesSection = document.getElementById('outcomes-section');
if (outcomeSelect) {
  outcomeSelect.addEventListener('change', () => {
    document.querySelectorAll('.outcome-panel').forEach(panel => {
      panel.style.display = panel.id === outcomeSelect.value ? '' : 'none';
    });
  });
  document.querySelectorAll('.outcome-toggle button').forEach(button => {
    button.addEventListener('click', () => {
      outcomesSection.classList.toggle('plot-mode', button.dataset.view === 'plot');
      document.querySelectorAll('.outcome-toggle button').forEach(b => {
        b.classList.toggle('active', b === button);
      });
    });
  });
}
</script>
""" if outcomes_html else ""
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>abench report</title>
<style>body{{font:15px system-ui;margin:2rem;color:#17212b}}.cards{{display:flex;gap:24px;overflow-x:auto}}article{{flex:1;min-width:420px;border:1px solid #ccc;padding:16px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}svg{{width:100%;min-width:420px}}table{{border-collapse:collapse;width:100%}}th,td{{padding:10px;text-align:left;border-bottom:1px solid #ddd}}.fastest{{background:#d7f3dc}}small{{color:#58616a}}.runtime{{max-width:1200px}}.scroll{{overflow-x:auto}}.outcome-toggle button{{margin-left:4px;border:1px solid #ccc;background:#f4f6f8;padding:4px 10px;border-radius:4px;cursor:pointer}}.outcome-toggle button.active{{background:#0072b2;color:#fff;border-color:#0072b2}}.plot-view{{display:none}}#outcomes-section.plot-mode .table-view{{display:none}}#outcomes-section.plot-mode .plot-view{{display:block}}</style>
<h1>abench report</h1><p>Whole-container cgroup v2 memory counts shared pages once, including file cache, kernel memory and the supervisor. Swap is recorded separately in memory.csv. Cache preparation and post-run summaries are excluded. Peak is the kernel high-water mark sampled during the model lifetime, including container startup. Memory panels use identical axes.</p>
{comparison_notes(runs)}
<label for="memory-component"><b>Highlight component:</b></label>
<select id="memory-component"><option value="">None</option>{options}</select>
<p>Selection applies to every memory chart. Each translucent band is one worker execution; darker overlaps indicate concurrent workers. Gaps remain unshaded. Hover over a band for its worker and time range.</p>
<noscript>Enable JavaScript to select and highlight component windows.</noscript>
<div class="cards">{"".join(cards)}</div><h2>Component runtimes</h2><p>Mean ± population standard deviation across worker executions, with observation count and maximum. These describe worker imbalance, not uncertainty across repeated experiments. Component timings exclude pipeline checkpoint writes; elapsed time includes startup, I/O and coordination. Parallel component times must not be summed to estimate wall time. Green cells mark the fastest successful experiment's mean; failed runs are excluded from winners.</p><div class="scroll"><table><tr><th>Component</th>{headers}</tr>{"".join(table)}</table></div><h2>Runtime comparison</h2><p>{legend}</p><div class="runtime">{runtime_chart(runs, components)}</div>{outcomes_html}{selector_script}{outcomes_script}</html>"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document)
    write_json(destination.with_suffix(".json"), runs)

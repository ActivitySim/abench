"""Optional GitHub publication, isolated from benchmark execution and credentials."""

import fcntl
import json
import re
import subprocess
import uuid
from pathlib import Path
from xml.etree import ElementTree as ET

from . import __version__
from .common import read_json, write_json
from .report import COLORS, escape, load_run, memory_chart, runtime_chart


def configuration(value, run_names):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"github"}:
        raise ValueError("publish requires a github mapping")
    config = value["github"]
    if not isinstance(config, dict) or set(config) - {"repository", "pr", "baseline"}:
        raise ValueError("invalid publish.github fields")
    repository = config.get("repository", "")
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise ValueError("publish.github.repository must be OWNER/REPO on github.com")
    if type(config.get("pr")) is not int or config["pr"] <= 0:
        raise ValueError("publish.github.pr must be a positive integer")
    baseline = config.get("baseline", next(iter(run_names)))
    if not isinstance(baseline, str) or baseline not in run_names:
        raise ValueError("publish.github.baseline must name a suite run")
    return {**config, "baseline": baseline}


def gh(*args, cwd=None):
    try:
        return subprocess.run(
            ["gh", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=cwd,
        ).stdout.strip()
    except FileNotFoundError as error:
        raise ValueError(
            "GitHub publication requires gh >= 2.99 with --attach"
        ) from error
    except subprocess.CalledProcessError as error:
        raise ValueError(
            f"GitHub publication failed: {error.stderr.strip()}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise ValueError(
            "GitHub publication timed out; retry with abench publish"
        ) from error


def preflight(config):
    """Read-only checks before expensive work; never upload in preflight."""
    if "--attach" not in gh("pr", "comment", "--help"):
        raise ValueError("GitHub image publication requires gh >= 2.99 with --attach")
    repository = config["repository"]
    permission = gh(
        "api",
        f"repos/{repository}",
        "--hostname",
        "github.com",
        "--jq",
        ".permissions.push",
    )
    if permission != "true":
        raise ValueError(
            "GitHub image uploads require repository write access; authenticate gh"
        )
    return json.loads(
        gh(
            "pr",
            "view",
            str(config["pr"]),
            "--repo",
            f"github.com/{repository}",
            "--json",
            "number,url,headRefOid,baseRefOid",
        )
    )


def cell(value):
    # Escape Markdown and suppress mentions from labels or model diagnostics.
    text = " ".join(str(value).splitlines())
    return "".join(
        f"&#{ord(char)};" if char in "\\`*_{}[]()#+!|@" else escape(char)
        for char in text
    )


def comparable(a, b):
    keys = (
        "households",
        "multiprocess",
        "processes",
        "sharrow",
        "profile_name",
        "platform",
        "memory",
        "shm_size",
        "config_overlay",
        "data_dir",
        "model_commit",
        "model_git_status",
    )
    return all(a["spec"].get(k) == b["spec"].get(k) for k in keys)


def delta(run, baseline, value):
    if (
        not baseline
        or not run["valid"]
        or not baseline["valid"]
        or not comparable(run, baseline)
    ):
        return "—"
    before, after = value(baseline), value(run)
    if before is None or after is None or before <= 0:
        return "—"
    return f"{(after / before - 1) * 100:+.1f}%"


def standalone(svg):
    _, _, width, height = ET.fromstring(svg).attrib["viewBox"].split()
    svg = svg.replace(
        "<svg ",
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        'style="background:white;font:12px sans-serif" ',
        1,
    )
    return svg.replace(
        ">", f'><rect width="{width}" height="{height}" fill="white"/>', 1
    )


def build_bundle(root):
    """Rebuild shareable artifacts from retained results, without GitHub access."""
    plan = read_json(root / "suite.json")
    if not plan:
        raise ValueError(f"No suite.json in {root}")
    config = configuration(
        plan["configuration"].get("publish"), [r["name"] for r in plan["runs"]]
    )
    if not config:
        raise ValueError("This suite has no publish.github configuration")
    bundle = root / "publication"
    bundle.mkdir(exist_ok=True)
    state_path = bundle / "state.json"
    state = read_json(state_path, {})
    if state and state["target"] != config:
        raise ValueError("Publication target differs from the saved publication state")
    if not state:
        state = {"target": config, "id": str(uuid.uuid4()), "status": "prepared"}
        write_json(state_path, state)
    marker = f"<!-- abench:{state['id']} -->"
    runs = []
    names = []
    for item in plan["runs"]:
        path = Path(item["output_dir"])
        if (path / "experiment.json").is_file():
            runs.append(load_run(path))
            names.append(item["name"])
    baseline = next(
        (run for name, run in zip(names, runs) if name == config["baseline"]), None
    )
    lines = [
        marker,
        "## abench results",
        "",
        f"Auto-generated by **abench {__version__}**.",
        "",
        f"PR: {config['repository']}#{config['pr']} · Baseline: {cell(config['baseline'])}",
    ]
    snapshot = plan.get("publication_pr", {})
    if snapshot:
        lines += [
            f"PR head at startup: `{snapshot['headRefOid']}`; base: `{snapshot['baseRefOid']}`."
        ]
    lines += [
        "",
        "| Run | Result | Elapsed (s) | Change | Peak (GiB) | Change |",
        "|---|---|---:|---:|---:|---:|",
    ]

    def elapsed(run):
        return run["status"].get("elapsed_seconds")

    for name, run in zip(names, runs):
        seconds = elapsed(run)
        time_text = f"{seconds:.2f}" if seconds is not None else "—"
        peak_text = f"{run['peak'] / 2**30:.3f}" if run["memory"] else "—"
        lines += [
            f"| {cell(name)} | {'valid' if run['valid'] else 'FAILED / INVALID'} | {time_text} | {delta(run, baseline, elapsed)} | {peak_text} | {delta(run, baseline, lambda r: r['peak'])} |"
        ]
    for item in plan["runs"]:
        if item["name"] not in names:
            lines += [
                f"| {cell(item['name'])} | NOT RUN / NO RESULTS | — | — | — | — |"
            ]
    lines += [
        "",
        "Negative changes mean lower elapsed time or memory. Changes are omitted for invalid runs or differing comparison settings. Single runs do not establish statistical significance.",
        "",
        "### Configuration and source commits",
    ]
    for name, run in zip(names, runs):
        spec = run["spec"]
        lines += [
            "",
            f"**{cell(name)}** — {cell(spec.get('label', name))}",
            f"Households: {cell(spec.get('households', 'unknown'))}; multiprocess: {cell(spec.get('multiprocess', False))}; processes: {cell(spec.get('processes', 1))}; Sharrow: {cell(spec.get('sharrow', False))}; profile: {cell(spec.get('profile_name', 'unknown'))}; platform: {cell(spec.get('platform', 'unknown'))}.",
        ]
        for source in spec.get("sources", []):
            lines += [
                f"- {cell(source['name'])}: {cell(source['repository'])} at `{cell(source['commit'])}`"
            ]
        if not run["valid"]:
            lines += [
                "- Invalid results are excluded from runtime comparisons and improvement claims."
            ]
    lines += [
        "",
        "### Memory traces",
        "",
        "![Memory traces](./memory.svg)",
        "",
        "Blue: total cgroup memory; green dashed: anonymous + shared memory when available. Panels share axes; invalid runs are labeled.",
        "",
        "### Component runtimes",
        "",
        "![Component runtimes](./runtimes.svg)",
        "",
        "Valid runs only. Mean ± population standard deviation across worker executions, not confidence intervals. Parallel component times must not be summed as wall time.",
        "",
        "The interactive HTML report and JSON remain in the experiment output directory.",
    ]
    (bundle / "comment.md").write_text("\n".join(lines) + "\n")
    xmax = (
        max((row["elapsed_seconds"] for r in runs for row in r["memory"]), default=1)
        or 1
    )
    ymax = (
        max((row["current_bytes"] for r in runs for row in r["memory"]), default=1) or 1
    )
    panels = []
    for i, (name, run) in enumerate(zip(names, runs)):
        fragment = memory_chart({**run, "component_windows": []}, xmax, ymax)
        svg = fragment[fragment.index("<svg") : fragment.index("</svg>") + 6]
        svg = svg.replace("<svg ", '<svg width="710" height="285" ', 1)
        panels += [
            f'<g transform="translate(0,{i * 325})"><text x="20" y="20">{escape(name)} — {"valid" if run["valid"] else "FAILED / INVALID"}</text><g transform="translate(0,30)">{svg}</g></g>'
        ]
    if not panels:
        panels = ['<text x="20" y="30">No memory results available</text>']
    memory = f'<svg viewBox="0 0 710 {max(1, len(runs)) * 325}">{"".join(panels)}</svg>'
    valid = [r for r in runs if r["valid"]]
    components = sorted({c for r in valid for c in r["components"]})
    chart = runtime_chart(valid, components)
    legend = "".join(
        f'<text x="20" y="{20 + i * 20}" fill="{COLORS[i % len(COLORS)]}">{escape(r["spec"]["label"])}</text>'
        for i, r in enumerate(valid)
    )
    height = 30 + 20 * max(1, len(valid))
    chart_height = float(ET.fromstring(chart).attrib["viewBox"].split()[-1])
    chart = chart.replace("<svg ", f'<svg width="900" height="{chart_height}" ', 1)
    legend = legend or '<text x="20" y="20">No valid runtime results</text>'
    runtime = f'<svg viewBox="0 0 900 {height + chart_height}">{legend}<g transform="translate(0,{height})">{chart}</g></svg>'

    for filename, svg in (("memory.svg", memory), ("runtimes.svg", runtime)):
        (bundle / filename).write_text(standalone(svg))
    return bundle, state


def publish(root, *, dry_run=False):
    """Publish once per suite; recover a successful comment after a lost response."""
    root = Path(root).expanduser().resolve()
    if not (root / "suite.json").is_file():
        raise ValueError(f"No suite.json in {root}")
    with (root / ".publication.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Publication is already running for this suite") from error
        bundle, state = build_bundle(root)
        if dry_run:
            print(f"Publication preview: {bundle / 'comment.md'}")
            return None
        if state.get("comment_url"):
            print(f"Already published: {state['comment_url']}")
            return state["comment_url"]
        try:
            config = state["target"]
            current = preflight(config)
            marker = f"<!-- abench:{state['id']} -->"
            comments = json.loads(
                gh(
                    "api",
                    f"repos/{config['repository']}/issues/{config['pr']}/comments",
                    "--hostname",
                    "github.com",
                    "--paginate",
                    "--slurp",
                )
            )
            found = next(
                (c for page in comments for c in page if marker in c["body"]), None
            )
            if not found:
                snapshot = read_json(root / "suite.json").get("publication_pr", {})
                body = (bundle / "comment.md").read_text()
                if snapshot and snapshot["headRefOid"] != current["headRefOid"]:
                    body += f"\nPR head has changed since startup to `{current['headRefOid']}`. Results describe the recorded commits above.\n"
                    (bundle / "comment.md").write_text(body)
                if len(body) > 60000:
                    raise ValueError(
                        "Comment exceeds the 60,000 character publication limit"
                    )
                for name in ("memory.svg", "runtimes.svg"):
                    if (bundle / name).stat().st_size > 10 * 1024**2:
                        raise ValueError(
                            f"{name} exceeds GitHub's 10 MB attachment limit"
                        )
                # Resolve relative image references from the bundle, including
                # output directories containing spaces or Markdown punctuation.
                (bundle / "upload.md").write_text(body)
                state["status"] = "posting"
                write_json(bundle / "state.json", state)
                url = gh(
                    "pr",
                    "comment",
                    str(config["pr"]),
                    "--repo",
                    f"github.com/{config['repository']}",
                    "--body-file",
                    str(bundle / "upload.md"),
                    "--attach",
                    "./memory.svg",
                    "--attach",
                    "./runtimes.svg",
                    cwd=bundle,
                )
                match = re.search(r"https://github\.com/[^\s]+#issuecomment-(\d+)", url)
                if not match:
                    raise ValueError(
                        "GitHub returned no comment URL; retry to reconcile publication"
                    )
                found = {"id": int(match[1]), "html_url": match[0]}
            state.update(
                status="published",
                comment_id=found["id"],
                comment_url=found["html_url"],
            )
            state.pop("error", None)
            write_json(bundle / "state.json", state)
        except (ValueError, OSError) as error:
            state.update(status="failed", error=str(error))
            write_json(bundle / "state.json", state)
            raise ValueError(
                f"{error}\nResults retained. Retry: abench publish {root}"
            ) from error
        print(f"Published: {state['comment_url']}")
        return state["comment_url"]

"""Optional GitHub publication, isolated from benchmark execution and credentials."""

import fcntl
import json
import re
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from urllib.parse import unquote, urlparse
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


def now():
    return datetime.now(timezone.utc).isoformat()


def local_checkout():
    """Find a development checkout, including when running in a uvx environment."""
    candidates = [Path(__file__).resolve().parents[2]]
    try:
        direct = json.loads(distribution("abench").read_text("direct_url.json") or "{}")
        url = urlparse(direct.get("url", ""))
        if url.scheme == "file" and url.netloc in ("", "localhost"):
            candidates.append(Path(unquote(url.path)))
    except (ValueError, OSError, PackageNotFoundError):
        pass
    return next(
        (
            str(p)
            for p in candidates
            if (p / "pyproject.toml").is_file() and (p / "src/abench").is_dir()
        ),
        None,
    )


def write_launcher(bundle, checkout):
    launcher = bundle / "publish.sh"
    fallback = ""
    if checkout:
        quoted = shlex.quote(checkout)
        fallback = f"""if command -v uvx >/dev/null 2>&1 && [ -f {quoted}/pyproject.toml ]; then
    exec uvx --refresh --from {quoted} abench publish "$suite_dir" "$@"
fi
"""
    launcher.write_text(
        """#!/bin/sh
# Permanent, safe-to-repeat abench publication launcher. No credentials stored.
set -eu
publication_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
suite_dir=$(dirname -- "$publication_dir")
if command -v abench >/dev/null 2>&1; then
    exec abench publish "$suite_dir" "$@"
fi
"""
        + fallback
        + """echo 'Cannot find abench. Install a version with PR publishing support, or restore the recorded checkout and install uv.' >&2
exit 127
"""
    )
    launcher.chmod(0o755)


STATUS_LABELS = {
    "prepared": "Not published",
    "failed": "Publication failed",
    "posting": "Publication outcome uncertain",
    "uncertain": "Publication outcome uncertain",
    "published": "Published",
    "missing": "Previously published comment not found",
}


def save_status(bundle, state):
    """JSON is authoritative; the Markdown file is a readable projection."""
    write_json(bundle / "state.json", state)
    label = STATUS_LABELS.get(state["status"], state["status"])
    target = state["target"]
    lines = [
        "# Publication status",
        "",
        f"**{label}**",
        "",
        f"Target: https://github.com/{target['repository']}/pull/{target['pr']}",
        f"Last publication attempt: {state.get('last_attempt_at', 'Never recorded')}",
    ]
    if state.get("comment_url"):
        lines += ["", f"[Open the recorded GitHub comment]({state['comment_url']})"]
    if state.get("last_verified_at"):
        lines += [f"Last GitHub check: {state['last_verified_at']}"]
    if state.get("error"):
        lines += ["", f"Last error: {cell(state['error'])}"]
    if state.get("verification_error"):
        lines += [
            "",
            f"GitHub verification failed: {cell(state['verification_error'])}",
        ]
    if state["status"] in ("posting", "uncertain"):
        lines += [
            "",
            "GitHub may have accepted the comment. Retry checks for the existing comment before posting again.",
        ]
    command = shlex.quote(str(bundle / "publish.sh"))
    lines += [
        "",
        "Publish or retry (safe to repeat):",
        "",
        "```sh",
        command,
        "```",
        "",
        "Preview with `--dry-run`; inspect local status with `--status`; check GitHub with `--verify`.",
        "",
        "This file records local knowledge; use --verify to check whether the comment still exists.",
    ]
    (bundle / "STATUS.md").write_text("\n".join(lines) + "\n")


def completion(bundle, state):
    if state["status"] == "published":
        print(f"Results published: {state['comment_url']}")
    elif state["status"] in ("posting", "uncertain"):
        print("Results saved; publication outcome uncertain.")
    else:
        print("Results saved but not published.")
    print(f"Publication status: {bundle / 'STATUS.md'}")
    if state["status"] != "published":
        print(f"Publish / retry: {shlex.quote(str(bundle / 'publish.sh'))}")


def find_comment(state):
    target = state["target"]
    pages = json.loads(
        gh(
            "api",
            f"repos/{target['repository']}/issues/{target['pr']}/comments",
            "--hostname",
            "github.com",
            "--paginate",
            "--slurp",
        )
    )
    marker = f"<!-- abench:{state['id']} -->"
    return next(
        (
            c
            for page in pages
            for c in page
            if marker in (c.get("body") or "") or c["id"] == state.get("comment_id")
        ),
        None,
    )


def verify_publication(bundle, state):
    """Read GitHub only; never recreate or edit a comment during verification."""
    try:
        found = find_comment(state)
    except (ValueError, OSError) as error:
        state["verification_error"] = str(error)
        save_status(bundle, state)
        raise
    state["last_verified_at"] = now()
    state.pop("verification_error", None)
    if found:
        state.update(
            status="published", comment_id=found["id"], comment_url=found["html_url"]
        )
        state.pop("error", None)
    elif state.get("comment_url"):
        state["status"] = "missing"
    save_status(bundle, state)


def prepare_publication(root):
    """Create local publication controls without reading benchmark measurements."""
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
        state["checkout"] = local_checkout()
        write_json(state_path, state)
    if "checkout" not in state:
        state["checkout"] = local_checkout()
    save_status(bundle, state)
    write_launcher(bundle, state.get("checkout"))
    return bundle, state


def build_bundle(root):
    """Rebuild shareable artifacts from retained results, without GitHub access."""
    bundle, state = prepare_publication(root)
    plan = read_json(root / "suite.json")
    config = state["target"]
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


def publish(root, *, dry_run=False, status_only=False, verify=False):
    """Publish once per suite; recover a successful comment after a lost response."""
    root = Path(root).expanduser().resolve()
    if not (root / "suite.json").is_file():
        raise ValueError(f"No suite.json in {root}")
    with (root / ".publication.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Publication is already running for this suite") from error
        if status_only or verify:
            bundle, state = prepare_publication(root)
            if verify:
                verify_publication(bundle, state)
            print((bundle / "STATUS.md").read_text())
            return state.get("comment_url")
        bundle, state = build_bundle(root)
        if dry_run:
            print(f"Publication preview: {bundle / 'comment.md'}")
            completion(bundle, state)
            return None
        if state.get("status") == "missing":
            raise ValueError(
                "The previously published comment was not found on GitHub; not reposting automatically. See publication/STATUS.md."
            )
        if state.get("comment_url"):
            completion(bundle, state)
            return state["comment_url"]
        try:
            state["last_attempt_at"] = now()
            save_status(bundle, state)
            config = state["target"]
            current = preflight(config)
            found = find_comment(state)
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
                state.update(status="posting", last_attempt_at=now())
                save_status(bundle, state)
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
                published_at=state.get("published_at") or now(),
                comment_id=found["id"],
                comment_url=found["html_url"],
            )
            state.pop("error", None)
            save_status(bundle, state)
        except (ValueError, OSError, KeyboardInterrupt) as error:
            state.update(
                status="uncertain"
                if state["status"] in ("posting", "uncertain")
                else "failed",
                error=str(error) or "Publication interrupted",
                last_attempt_at=state.get("last_attempt_at") or now(),
            )
            save_status(bundle, state)
            completion(bundle, state)
            if isinstance(error, KeyboardInterrupt):
                raise
            raise ValueError(
                f"{error}\nResults retained. Retry: {shlex.quote(str(bundle / 'publish.sh'))}"
            ) from error
        completion(bundle, state)
        return state["comment_url"]

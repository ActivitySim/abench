"""Named experiment suites with shared options and safe string substitution."""

import io
import re
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .assets import plan_assets, prepare_assets
from .common import write_json
from .inputs import resolve_inputs
from .report import report
from .sources import suite_source

OPTIONS = {
    "model_dir",
    "profile",
    "sources",
    "activitysim_commit",
    "sharrow_commit",
    "multiprocess",
    "processes",
    "sharrow",
    "households",
    "warmup_households",
    "cache_retries",
    "data_dir",
    "config_overlay",
    "cache_from",
    "flow_cache_dir",
    "reuse_flows",
    "label",
    "compare",
    "interval",
    "memory",
    "shm_size",
    "platform",
}
PATHS = {
    "model_dir",
    "data_dir",
    "cache_from",
    "flow_cache_dir",
    "config_overlay",
    "compare",
}
LISTS = {"config_overlay", "compare"}
TOKEN = re.compile(r"\$\{([^{}]+)\}")


class SuiteLoader(yaml.SafeLoader):
    """Reject duplicate keys so an accidental repeated default cannot disappear."""


def unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise ValueError("experiment configuration keys must be strings")
        if key in result:
            raise ValueError(f"duplicate configuration key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


SuiteLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping
)


def expand_variables(document, timestamp, inputs=None):
    """Resolve named terms recursively; never execute shell code or read env vars."""
    terms = document.get("vars", {})
    if not isinstance(terms, dict):
        raise ValueError("vars must be a mapping")
    if "timestamp" in terms:
        raise ValueError("timestamp is a reserved variable")
    resolved = {**(inputs or {}), "timestamp": timestamp}

    def term(name, stack):
        if name in resolved:
            return resolved[name]
        if name in stack:
            raise ValueError(f"cyclic variable: {' -> '.join((*stack, name))}")
        if name not in terms:
            raise ValueError(f"undefined variable: {name}")
        value = terms[name]
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"variable {name} must be a scalar")
        resolved[name] = expand(value, (*stack, name))
        return resolved[name]

    def expand(value, stack=()):
        if isinstance(value, str):
            match = TOKEN.fullmatch(value)
            if match:
                return term(match[1], stack)
            return TOKEN.sub(lambda m: str(term(m[1], stack)), value)
        if isinstance(value, list):
            return [expand(v, stack) for v in value]
        if isinstance(value, dict):
            return {k: expand(v, stack) for k, v in value.items()}
        return value

    for name in terms:
        term(name, ())
    return {k: v if k == "inputs" else expand(v) for k, v in document.items()}


def merge_options(defaults, overrides, resolutions=None):
    """Runs replace ordinary defaults; source pins merge by distribution name."""
    if not isinstance(overrides, dict):
        raise ValueError("defaults and each run must be option mappings")
    unknown = set(overrides) - OPTIONS
    if unknown:
        raise ValueError(f"unknown experiment options: {sorted(unknown)}")
    merged = dict(defaults, **overrides)
    if resolutions is None:
        resolutions = {}
    if "sources" in overrides:
        if not isinstance(overrides["sources"], list):
            raise ValueError("sources must be a list")
        pins = {item["name"]: item for item in defaults.get("sources", [])}
        seen = set()
        for value in overrides["sources"]:
            item = suite_source(value, resolutions)
            if item["name"] in seen:
                raise ValueError(f"duplicate source: {item['name']}")
            seen.add(item["name"])
            pins[item["name"]] = item
        merged["sources"] = list(pins.values())
    return merged


def arguments(options, base):
    """Translate typed YAML options into the existing CLI's validation interface."""
    argv = []
    options = dict(options)
    options.setdefault("model_dir", str(base))
    for key, value in options.items():
        if value is None:
            continue
        if key in ("multiprocess", "sharrow", "reuse_flows"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a YAML boolean")
            argv.append(
                ("--multiprocess" if value else "--single-process")
                if key == "multiprocess"
                else ("--" if value else "--no-") + key.replace("_", "-")
            )
            continue
        if key == "sources":
            for item in value:
                extras = "[" + ",".join(item["extras"]) + "]" if item["extras"] else ""
                subdir = (
                    "#subdirectory=" + item["subdirectory"]
                    if item["subdirectory"]
                    else ""
                )
                argv += [
                    "--source",
                    f"{item['name']}{extras}={item['repository']}@{item['commit']}{subdir}",
                ]
            continue
        values = value if key in LISTS else [value]
        if not isinstance(values, list) or any(
            not isinstance(v, (str, int, float)) or isinstance(v, bool) for v in values
        ):
            raise ValueError(f"invalid value for {key}")
        if not values:
            continue
        if key in PATHS or (key == "profile" and value not in ("mtc", "sandag")):
            values = [str((base / Path(v).expanduser()).resolve()) for v in values]
        argv += ["--" + key.replace("_", "-"), *map(str, values)]
    return argv


def read_suite(path):
    """Read and validate the file envelope without resolving inputs or sources."""
    path = path.expanduser().resolve()
    try:
        raw = path.read_text()
        document = yaml.load(raw, Loader=SuiteLoader)
    except yaml.YAMLError as error:
        raise ValueError(f"invalid experiment YAML: {error}") from error
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("experiment file requires schema_version: 1")
    unknown = set(document) - {
        "schema_version",
        "vars",
        "inputs",
        "defaults",
        "runs",
        "output_root",
        "data_assets",
    }
    if unknown:
        raise ValueError(f"unknown experiment file fields: {sorted(unknown)}")
    return raw, document


def load_suite(path, assignments=()):
    """Resolve typed inputs and expand the suite before any external side effects."""
    path = path.expanduser().resolve()
    raw, document = read_suite(path)
    input_values, cli_overrides = resolve_inputs(document, assignments)
    document = expand_variables(
        document, datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f"), input_values
    )
    assets = plan_assets(document.get("data_assets"), path.parent)
    resolutions = {}
    defaults = merge_options({}, document.get("defaults", {}), resolutions)
    runs = document.get("runs")
    if not isinstance(runs, dict) or not runs:
        raise ValueError("runs must be a nonempty mapping of run names to options")
    output = document.get("output_root")
    if not isinstance(output, str) or not output:
        raise ValueError("output_root must be a path")
    output = (path.parent / Path(output).expanduser()).resolve()
    if output.exists():
        raise ValueError(
            f"output_root already exists: {output}; use ${{timestamp}} for repeatable launches"
        )
    plan = []
    for name, overrides in runs.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise ValueError(f"invalid run name: {name!r}")
        options = merge_options(defaults, overrides, resolutions)
        options.setdefault("label", name)
        argv = arguments(options, path.parent)
        destination = output / name
        plan.append(
            {
                "name": name,
                "argv": argv + ["--output-dir", str(destination)],
                "output_dir": str(destination),
            }
        )
    return {
        "source_file": str(path),
        "original_yaml": raw,
        "cli_overrides": cli_overrides,
        "input_values": input_values,
        "configuration": document,
        "output_root": str(output),
        "runs": plan,
        "data_assets": assets,
        "source_resolutions": list(resolutions.values()),
    }


def run_suite(path, invoke, validate_only=False, prepare_only=False, assignments=()):
    """Preflight every run, execute serially, and preserve partial failure reports."""
    plan = load_suite(path, assignments=assignments)
    root = Path(plan["output_root"])
    if not validate_only:
        prepare_assets(plan["data_assets"])
    if prepare_only:
        print(f"Prepared {len(plan['data_assets'])} assets; no experiments started")
        return 0
    # Validation uses the same CLI checks as individual runs. Input preparation
    # above runs only for execution/prepare; validate itself creates nothing.
    # Run it for the whole suite first, so a typo in run two cannot waste run one.
    for run in plan["runs"]:
        with redirect_stdout(io.StringIO()):
            code = invoke(["validate", *run["argv"]])
        if code:
            return code
    if validate_only:
        print(f"Validated {len(plan['runs'])} experiments; output root: {root}")
        return 0
    root.mkdir(parents=True)
    (root / "experiments.yaml").write_text(plan["original_yaml"])
    write_json(root / "suite.json", plan)
    completed = []
    try:
        for run in plan["runs"]:
            print(f"Running experiment {run['name']}…", flush=True)
            try:
                code = invoke(["run", *run["argv"]])
            finally:
                # Failed builds/models still have an experiment record and belong
                # in the comparison, with failure status rather than winner badges.
                if (Path(run["output_dir"]) / "experiment.json").is_file():
                    completed.append(Path(run["output_dir"]))
            if code:
                return code
    finally:
        if completed:
            report(completed, root / "comparison.html")
            print(f"Comparison: {root / 'comparison.html'}", flush=True)
    return 0

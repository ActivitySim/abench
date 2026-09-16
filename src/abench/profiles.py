"""Declarative model layouts and inexpensive preflight checks."""

import csv
from pathlib import Path, PurePosixPath

import yaml

FIELDS = {
    "schema_version",
    "name",
    "configs",
    "mp_configs",
    "snapshot",
    "data_dir",
    "required_inputs",
    "household_table",
    "extensions",
    "adapter",
    "settings",
    "models_from",
    "exclude_models",
    "mp_settings",
    "input_tables",
    "output_tables",
    "output_prefix",
    "zone_label",
    "sources",
    "requirements",
    "constraints",
    "python_image",
}


def relative_path(value):
    """Snapshot and config paths must resolve within the supplied model checkout."""
    if (
        not isinstance(value, str)
        or not value
        or PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
        or "\\" in value
    ):
        raise ValueError(f"expected a relative model path: {value!r}")
    return value


def load_profile(value, root):
    """Load a model-owned YAML profile or one of the shipped example profiles."""
    path = Path(value).expanduser()
    if value in ("mtc", "mtc-extended", "sandag"):
        path = Path(__file__).parent / "profiles" / f"{value}.yaml"
    elif not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise ValueError(f"profile not found: {path}")
    profile = yaml.safe_load(path.read_text())
    if not isinstance(profile, dict) or profile.get("schema_version") != 1:
        raise ValueError("profile must be a mapping with schema_version: 1")
    unknown = set(profile) - FIELDS
    if unknown:
        raise ValueError(f"unknown profile fields: {sorted(unknown)}")
    if not isinstance(profile.get("name"), str) or not profile["name"]:
        raise ValueError("profile needs a name")
    for key in ("configs", "snapshot"):
        if not isinstance(profile.get(key), list) or not profile[key]:
            raise ValueError(f"profile needs a nonempty {key} list")
    for key in ("configs", "mp_configs", "snapshot"):
        for item in profile.get(key, []):
            relative_path(item)
    for key in ("models_from", "mp_settings"):
        if profile.get(key):
            relative_path(profile[key])
    for key in ("extensions", "exclude_models", "requirements", "constraints"):
        if not isinstance(profile.get(key, []), list) or any(
            not isinstance(v, str) for v in profile.get(key, [])
        ):
            raise ValueError(f"{key} must be a list of strings")
    if not isinstance(profile.get("settings", {}), dict):
        raise ValueError("settings must be a mapping")
    if profile.get("adapter") and (
        not isinstance(profile["adapter"], str) or profile["adapter"].count(":") != 1
    ):
        raise ValueError("adapter must be module:function")
    for key in ("input_tables", "output_tables"):
        tables = profile.get(key, {})
        if not isinstance(tables, dict) or any(
            not isinstance(opts, dict) for opts in tables.values()
        ):
            raise ValueError(f"{key} must map table names to summary options")
    return profile


def validate_model(profile, root, data, households):
    """Check the snapshot closure and required files before starting Docker builds."""
    if not data.is_dir():
        raise ValueError(f"data directory not found: {data}")
    snapshots = [root / item for item in profile["snapshot"]]
    for path in snapshots:
        if not path.exists() or not path.resolve().is_relative_to(root):
            raise ValueError(f"missing or external snapshot path: {path}")
    for i, path in enumerate(snapshots):
        if any(
            path.is_relative_to(other) or other.is_relative_to(path)
            for other in snapshots[:i]
        ):
            raise ValueError("snapshot paths must not overlap")
        # Do not accidentally copy external data through a directory symlink.
        if path.is_dir() and any(p.is_symlink() for p in path.rglob("*")):
            raise ValueError(f"snapshot contains symlinks; use ordinary files: {path}")
    for name in (
        profile["configs"]
        + profile.get("mp_configs", [])
        + [profile[k] for k in ("models_from", "mp_settings") if profile.get(k)]
    ):
        path = root / name
        if not path.exists() or not any(
            path.is_relative_to(item) for item in snapshots
        ):
            raise ValueError(f"config must exist and be included in snapshot: {name}")
    for group in profile.get("required_inputs", []):
        choices = [group] if isinstance(group, str) else group
        if not isinstance(choices, list) or not choices:
            raise ValueError(
                "required_inputs entries must be paths or nonempty alternative lists"
            )
        if not any((data / relative_path(name)).is_file() for name in choices):
            raise ValueError(f"missing required input; expected one of: {choices}")
    # Parquet counts are checked against realized outputs after the run. CSV
    # counting uses streaming and no host pandas dependency.
    household_file = data / profile.get("household_table", "households.csv")
    if households > 0 and household_file.suffix == ".csv" and household_file.is_file():
        with household_file.open(newline="") as stream:
            reader = csv.reader(stream)
            next(reader, None)
            count = sum(1 for row in reader if row)
        if households > count:
            raise ValueError(
                f"requested {households:,} households but only {count:,} are available"
            )

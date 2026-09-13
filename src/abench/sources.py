"""Normalize exact GitHub source overrides before executing any build commands."""

import re
from pathlib import PurePosixPath


def canonical_name(name):
    """Compare distribution names using Python packaging's normalization rules."""
    return re.sub(r"[-_.]+", "-", name).lower()


def source(value):
    """Accept a CLI shorthand or a profile mapping, including extras/subdirectory."""
    if isinstance(value, str):
        match = re.fullmatch(
            r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[([A-Za-z0-9_,.-]+)\])?=([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-fA-F]{40})(?:#subdirectory=([^\s]+))?",
            value,
        )
        if not match:
            raise ValueError(
                "source must be distribution[extras]=organization/repository@full-SHA[#subdirectory=path]"
            )
        name, extras, repository, commit, subdirectory = match.groups()
        value = dict(
            name=name,
            repository=repository,
            commit=commit,
            extras=extras.split(",") if extras else [],
            subdirectory=subdirectory or "",
        )
    if not isinstance(value, dict) or set(value) - {
        "name",
        "repository",
        "commit",
        "extras",
        "subdirectory",
    }:
        raise ValueError("invalid source fields")
    result = dict(value)
    for key, pattern in [
        ("name", r"[A-Za-z0-9][A-Za-z0-9_.-]*"),
        ("repository", r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
        ("commit", r"[0-9a-fA-F]{40}"),
    ]:
        if not isinstance(result.get(key), str) or not re.fullmatch(
            pattern, result[key]
        ):
            raise ValueError(f"invalid source {key}: {result.get(key)!r}")
    result["name"] = canonical_name(result["name"])
    result["commit"] = result["commit"].lower()
    result.setdefault("subdirectory", "")
    result.setdefault("extras", [])
    subdir = result["subdirectory"]
    if (
        not isinstance(subdir, str)
        or PurePosixPath(subdir).is_absolute()
        or ".." in PurePosixPath(subdir).parts
        or "\\" in subdir
    ):
        raise ValueError("source subdirectory must stay inside the checkout")
    if not isinstance(result["extras"], list) or any(
        not isinstance(e, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", e)
        for e in result["extras"]
    ):
        raise ValueError("invalid package extras")
    result["extras"] = sorted(set(result["extras"]))
    return result


def resolve_sources(defaults, overrides, activitysim=None, sharrow=None):
    """CLI entries replace profile pins; aliases cannot silently contradict them."""
    selected = {}
    for value in defaults:
        item = source(value)
        if item["name"] in selected:
            raise ValueError(f"duplicate profile source: {item['name']}")
        selected[item["name"]] = item
    explicit = {}
    for value in overrides:
        item = source(value)
        if item["name"] in explicit:
            raise ValueError(f"duplicate CLI source: {item['name']}")
        explicit[item["name"]] = item
    for name, sha in [("activitysim", activitysim), ("sharrow", sharrow)]:
        if sha:
            item = source(f"{name}=ActivitySim/{name}@{sha}")
            if name in explicit and explicit[name] != item:
                raise ValueError(f"conflicting --source and --{name}-commit")
            explicit[name] = item
    selected.update(explicit)
    if "activitysim" not in selected:
        raise ValueError("provide an exact ActivitySim source commit")
    return [selected[name] for name in sorted(selected)]

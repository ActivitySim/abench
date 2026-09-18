"""Normalize exact GitHub source overrides before executing any build commands."""

import re
import subprocess
from pathlib import PurePosixPath


def canonical_name(name):
    """Compare distribution names using Python packaging's normalization rules."""
    return re.sub(r"[-_.]+", "-", name).lower()


def suite_source(value, resolutions):
    """Pin a suite's GitHub branch or PR head once, before any builds begin.

    Only named suites accept moving selectors. The CLI, model profiles, and
    Docker builder continue to receive immutable commits. A shared memo prevents
    a branch update between runs from changing an inherited dependency.
    """
    if not isinstance(value, dict) or not ({"branch", "pr"} & value.keys()):
        return source(value)
    if sum(key in value for key in ("commit", "branch", "pr")) != 1:
        raise ValueError("source requires exactly one of commit, branch, or pr")
    selector = "branch" if "branch" in value else "pr"
    requested = value[selector]
    if selector == "pr":
        if type(requested) is not int or requested <= 0:
            raise ValueError("source pr must be a positive integer")
        ref = f"refs/pull/{requested}/head"
    else:
        if (
            not isinstance(requested, str)
            or not requested
            or any(c.isspace() or c in "~^:?*[\\" for c in requested)
            or any(ord(c) < 32 or ord(c) == 127 for c in requested)
            or ".." in requested
            or "@{" in requested
            or requested.endswith(".")
            or any(
                not p or p.startswith(".") or p.endswith(".lock")
                for p in requested.split("/")
            )
        ):
            raise ValueError("invalid source branch")
        ref = f"refs/heads/{requested}"
    # Reuse exact-source validation before allowing any repository into Git.
    pin = source(
        {k: v for k, v in value.items() if k != selector} | {"commit": "0" * 40}
    )
    repository = pin["repository"]
    key = (repository.lower(), ref)
    if key not in resolutions:
        try:
            result = subprocess.run(
                [
                    "git",
                    "ls-remote",
                    "--exit-code",
                    f"https://github.com/{repository}.git",
                    ref,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(
                f"Cannot resolve {repository} {ref}: {error}. "
                "Check Git/network access and the branch or PR number, or use commit: <full-SHA>."
            ) from error
        matches = [line.split() for line in result.stdout.splitlines()]
        commits = [parts[0] for parts in matches if len(parts) == 2 and parts[1] == ref]
        if len(commits) != 1 or not re.fullmatch(r"[0-9a-fA-F]{40}", commits[0]):
            raise ValueError(
                f"GitHub did not return an exact commit for {repository} {ref}"
            )
        resolutions[key] = {
            "repository": repository,
            "ref": ref,
            "commit": commits[0].lower(),
        }
        print(f"Resolved {repository} {ref} → {commits[0].lower()}", flush=True)
    pin["commit"] = resolutions[key]["commit"]
    return pin


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

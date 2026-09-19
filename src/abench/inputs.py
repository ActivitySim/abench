"""Typed public inputs for experiment suites; internal vars remain read-only."""

import math
import re

from .menus import choose

TYPES = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
}


def check_value(name, spec, value):
    """Validate YAML defaults and converted overrides using identical rules."""
    kind = spec["type"]
    if type(value) not in TYPES[kind]:
        raise ValueError(f"input {name} must be {kind}")
    if kind == "number" and isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"input {name} must be finite")
    if "choices" in spec and value not in spec["choices"]:
        raise ValueError(f"input {name} must be one of {spec['choices']!r}")
    for key, invalid in (
        ("minimum", lambda a, b: a < b),
        ("maximum", lambda a, b: a > b),
    ):
        if key in spec and invalid(value, spec[key]):
            raise ValueError(f"input {name} must satisfy {key}: {spec[key]}")
    return value


def input_schema(document):
    """Validate declarations without requiring values, network, or file writes."""
    inputs = document.get("inputs", {})
    terms = document.get("vars", {})
    if not isinstance(inputs, dict) or not isinstance(terms, dict):
        raise ValueError("inputs and vars must be mappings")
    if "timestamp" in inputs or "timestamp" in terms:
        raise ValueError("timestamp is a reserved variable")
    if inputs.keys() & terms.keys():
        raise ValueError("inputs and vars must not declare the same name")
    for name, spec in inputs.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"invalid input name: {name!r}")
        if not isinstance(spec, dict) or set(spec) - {
            "type",
            "default",
            "required",
            "description",
            "choices",
            "minimum",
            "maximum",
        }:
            raise ValueError(f"invalid declaration for input {name}")
        if not isinstance(spec.get("type"), str) or spec["type"] not in TYPES:
            raise ValueError(
                f"input {name} requires type: string, integer, number, or boolean"
            )
        required = spec.get("required", False)
        if type(required) is not bool:
            raise ValueError(f"input {name} required must be boolean")
        if required == ("default" in spec):
            raise ValueError(
                f"input {name} must have either a default or required: true"
            )
        if "description" in spec and not isinstance(spec["description"], str):
            raise ValueError(f"input {name} description must be a string")
        for key in ("minimum", "maximum"):
            if key in spec and (
                spec["type"] not in ("integer", "number")
                or type(spec[key]) not in (int, float)
                or (isinstance(spec[key], float) and not math.isfinite(spec[key]))
            ):
                raise ValueError(
                    f"input {name} {key} must be a finite numeric bound for a numeric input"
                )
        if (
            "minimum" in spec
            and "maximum" in spec
            and spec["minimum"] > spec["maximum"]
        ):
            raise ValueError(f"input {name} minimum exceeds maximum")
        if "choices" in spec:
            if not isinstance(spec["choices"], list) or not spec["choices"]:
                raise ValueError(f"input {name} choices must be a nonempty list")
            for choice in spec["choices"]:
                check_value(name, spec, choice)
        if "default" in spec:
            check_value(name, spec, spec["default"])
    return inputs


def parse_value(name, spec, text):
    """Convert terminal or CLI text using the declared scalar type."""
    kind = spec["type"]
    try:
        if kind == "integer":
            if not re.fullmatch(r"[+-]?[0-9]+", text):
                raise ValueError()
            value = int(text)
        elif kind == "number":
            value = float(text)
        elif kind == "boolean":
            if text.lower() not in ("true", "false"):
                raise ValueError()
            value = text.lower() == "true"
        else:
            value = text
    except ValueError as error:
        raise ValueError(f"input {name} must be {kind}; got {text!r}") from error
    return check_value(name, spec, value)


def resolve_inputs(document, assignments, interactive=False):
    """Parse NAME=VALUE as declared scalar types, never as arbitrary YAML."""
    schema = input_schema(document)
    explicit = {}
    for assignment in assignments:
        name, separator, text = assignment.partition("=")
        if not separator or not name:
            raise ValueError("--set requires NAME=VALUE")
        if name not in schema:
            raise ValueError(
                f"unknown input {name!r}; --set can only override declared inputs"
            )
        if name in explicit:
            raise ValueError(f"duplicate --set input: {name}")
        explicit[name] = parse_value(name, schema[name], text)
    values = {}
    for name, spec in schema.items():
        if name in explicit:
            values[name] = explicit[name]
        elif interactive:
            values[name] = prompt_value(name, spec)
        elif "default" in spec:
            values[name] = spec["default"]
        else:
            raise ValueError(
                f"missing required input {name}; run in a terminal to answer prompts "
                f"or supply --set {name}=VALUE"
            )
    return values, explicit


def input_help(document):
    """Describe the file's interface even when required inputs are missing."""
    lines = [
        "Experiment inputs (prompted at startup; optionally override with --set NAME=VALUE):"
    ]
    for name, spec in input_schema(document).items():
        status = "required" if spec.get("required") else f"default: {spec['default']!r}"
        constraints = [
            f"{key}: {spec[key]}"
            for key in ("choices", "minimum", "maximum")
            if key in spec
        ]
        lines.append(
            f"  {name} ({spec['type']}; {status})"
            + ("; " + "; ".join(constraints) if constraints else "")
        )
        if spec.get("description"):
            lines.append(f"    {spec['description']}")
    if len(lines) == 1:
        lines.append("  No inputs declared.")
    return "\n".join(lines)


def prompt_value(name, spec):
    """Keep asking until the user supplies a valid value or accepts a default."""
    if spec.get("description"):
        print(f"{name}: {spec['description']}", flush=True)
    if "choices" in spec:
        options = spec["choices"]
        default_index = options.index(spec["default"]) if "default" in spec else 0
        selected = choose(name, [str(value) for value in options], default_index)
        return check_value(name, spec, options[selected])
    constraints = ", ".join(
        f"{key}: {spec[key]}"
        for key in ("choices", "minimum", "maximum")
        if key in spec
    )
    suffix = f" [{spec['default']}]" if "default" in spec else " (required)"
    prompt = f"  {name} ({spec['type']}{'; ' + constraints if constraints else ''}){suffix}: "
    while True:
        try:
            text = input(prompt)
        except EOFError as error:
            raise ValueError(
                f"Input ended while asking for {name}; experiment not started"
            ) from error
        if not text.strip():
            if "default" in spec:
                return spec["default"]
            print(f"  {name} is required; enter a value.", flush=True)
            continue
        try:
            return parse_value(name, spec, text)
        except ValueError as error:
            print(f"  {error}. Try again.", flush=True)

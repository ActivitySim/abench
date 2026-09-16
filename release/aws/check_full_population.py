#!/usr/bin/env python3
"""Require a full-population run to retain every input household."""

import argparse
import json
from pathlib import Path


def rows(path: Path) -> int:
    try:
        document = json.loads(path.read_text())
        value = document["households"]["rows"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read household rows from {path}: {error}") from error
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid household row count in {path}: {value!r}")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="abench experiment directory")
    args = parser.parse_args(argv)
    phase = args.run / "measured"
    input_rows = rows(phase / "input-summary.json")
    output_rows = rows(phase / "output-summary.json")
    if input_rows != output_rows:
        print(
            f"household population mismatch: input={input_rows:,}, "
            f"output={output_rows:,}"
        )
        return 1
    print(f"full population retained: {input_rows:,} households")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

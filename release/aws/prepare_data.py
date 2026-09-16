#!/usr/bin/env python3
"""Download and verify the canonical full-scale MTC and SANDAG data."""

import argparse
import sys
from pathlib import Path

from activitysim.cli.create import download_asset

MTC_DATA_URL = (
    "https://github.com/ActivitySim/activitysim-prototype-mtc/"
    "releases/download/v1.3.4/data_full.tar.zst"
)
MTC_DATA_SHA256 = "b402506a61055e2d38621416dd9a5c7e3cf7517c0a9ae5869f6d760c03284ef3"


def prepare_mtc(model: Path, cache: Path) -> None:
    download_asset(
        MTC_DATA_URL,
        model / "data_full.tar.zst",
        sha256=MTC_DATA_SHA256,
        link=cache / "mtc",
        base_path=model,
        unpack=model / "data_full",
    )


def prepare_sandag(model: Path) -> None:
    scripts = model / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        from fulldata import get_full_data

        get_full_data(model / "data-full")
    finally:
        sys.path.remove(str(scripts))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("mtc-extended", "sandag"))
    parser.add_argument("repository", type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    args = parser.parse_args(argv)
    args.cache.mkdir(parents=True, exist_ok=True)
    repository = args.repository.resolve()
    if args.model == "mtc-extended":
        prepare_mtc(repository, args.cache.resolve())
    else:
        prepare_sandag(repository)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Download and verify the canonical full-scale MTC and SANDAG data."""

import argparse
import subprocess
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
        unpack="data_full",
    )


def fetch_s3_cache(source: str, destination: str) -> None:
    subprocess.run(
        ["aws", "s3", "sync", source, destination, "--only-show-errors"], check=True
    )


def prepare_sandag(model: Path, s3_cache_uri: str | None) -> None:
    scripts = model / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        from fulldata import get_full_data

        full_data_dir = model / "data-full"
        full_data_dir.mkdir(parents=True, exist_ok=True)
        if s3_cache_uri:
            print(f"checking data cache {s3_cache_uri}")
            fetch_s3_cache(s3_cache_uri.rstrip("/") + "/", str(full_data_dir))
        get_full_data(full_data_dir)
    finally:
        sys.path.remove(str(scripts))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=("mtc-extended", "sandag"))
    parser.add_argument("repository", type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument(
        "--s3-cache-uri",
        help="s3://bucket/prefix of a pre-populated, read-only cache of the extracted data",
    )
    args = parser.parse_args(argv)
    args.cache.mkdir(parents=True, exist_ok=True)
    repository = args.repository.resolve()
    if args.model == "mtc-extended":
        prepare_mtc(repository, args.cache.resolve())
    else:
        prepare_sandag(repository, args.s3_cache_uri)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

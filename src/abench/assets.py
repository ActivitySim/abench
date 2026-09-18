"""Checksum-verified assets using ActivitySim's external-example cache layout."""

import fcntl
import gzip
import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import platformdirs

from .common import read_json, write_json


def relative(value):
    """Asset paths must stay within their chosen cache and destination roots."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"invalid asset path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) == ".":
        raise ValueError(f"asset path must be relative without '..': {value!r}")
    return str(path)


def plan_assets(config, base):
    """Resolve declarations without network access or filesystem mutations."""
    if config is None:
        return []
    if not isinstance(config, dict) or set(config) - {"name", "cache_dir", "assets"}:
        raise ValueError("data_assets accepts name, cache_dir, and assets")
    cache = Path(platformdirs.user_cache_dir("ActivitySim")) / "External-Examples"
    if "cache_dir" in config and (
        not isinstance(config["cache_dir"], str) or not config["cache_dir"]
    ):
        raise ValueError("data_assets.cache_dir must be a nonempty path string")
    if config.get("cache_dir"):
        cache = (base / Path(config["cache_dir"]).expanduser()).resolve()
    if config.get("name"):
        cache /= relative(config["name"])
    assets = config.get("assets")
    if not isinstance(assets, dict) or not assets:
        raise ValueError("data_assets.assets must be a nonempty mapping")
    result = []
    for name, info in assets.items():
        name = relative(name)
        if not isinstance(info, dict) or set(info) - {"url", "sha256", "unpack"}:
            raise ValueError(f"asset {name} accepts url, sha256, and unpack")
        url, digest = info.get("url"), info.get("sha256")
        if not isinstance(url, str) or urlparse(url).scheme not in {"https", "http"}:
            raise ValueError(f"asset {name} needs an HTTP(S) URL")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdefABCDEF" for c in digest)
        ):
            raise ValueError(f"asset {name} needs a full SHA-256 checksum")
        unpack = relative(info["unpack"]) if info.get("unpack") else None
        if unpack and not name.endswith((".zip", ".tar.gz", ".tar.zst")):
            raise ValueError(f"unsupported asset archive: {name}")
        destination = base / (unpack or name)
        if not destination.parent.resolve().is_relative_to(base.resolve()):
            raise ValueError(
                f"asset destination parent escapes suite directory: {destination}"
            )
        if any(
            destination == Path(x["destination"])
            or destination in Path(x["destination"]).parents
            or Path(x["destination"]) in destination.parents
            for x in result
        ):
            raise ValueError("asset destinations must not overlap")
        result.append(
            dict(
                name=name,
                url=url,
                sha256=digest.lower(),
                unpack=unpack,
                cache_file=str(cache / name),
                destination=str(destination),
            )
        )
    return result


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(asset, cached):
    """Never expose a partial or unverified download at ActivitySim's cache path."""
    if cached.is_file() and checksum(cached) == asset["sha256"]:
        print(f"Using cached asset: {cached}", flush=True)
        return
    print(f"Downloading asset: {asset['url']}", flush=True)
    with tempfile.TemporaryDirectory(dir=cached.parent) as temporary:
        raw = Path(temporary) / "download"
        with (
            urllib.request.urlopen(asset["url"], timeout=120) as response,
            raw.open("wb") as stream,
        ):
            shutil.copyfileobj(response, stream)
        ready = raw
        # ActivitySim hashes the decompressed file for a .gz URL whose target
        # filename lacks .gz. Archive checksums instead cover the archive bytes.
        if asset["url"].endswith(".gz") and not cached.name.endswith(".gz"):
            ready = Path(temporary) / "decoded"
            with gzip.open(raw, "rb") as source, ready.open("wb") as stream:
                shutil.copyfileobj(source, stream)
        if checksum(ready) != asset["sha256"]:
            raise ValueError(
                f"asset checksum mismatch: {asset['name']}; expected {asset['sha256']}"
            )
        os.replace(ready, cached)


def extract(archive, destination):
    """Stream regular files only; reject links and archive path traversal."""

    def target(name):
        if name.rstrip("/") in {"", "."}:
            return destination
        return destination / relative(name.rstrip("/"))

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                path = target(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode not in (0, 0o100000, 0o040000):
                    raise ValueError(
                        "asset archives cannot contain links or special files"
                    )
                if info.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as source, path.open("wb") as stream:
                        shutil.copyfileobj(source, stream)
    else:
        import contextlib

        import zstandard

        with contextlib.ExitStack() as stack:
            source = stack.enter_context(archive.open("rb"))
            if archive.name.endswith(".tar.zst"):
                source = stack.enter_context(
                    zstandard.ZstdDecompressor().stream_reader(source)
                )
            tar = stack.enter_context(tarfile.open(fileobj=source, mode="r|*"))
            for member in tar:
                path = target(member.name)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as incoming, path.open("wb") as stream:
                        shutil.copyfileobj(incoming, stream)
                else:
                    raise ValueError(
                        "asset archives cannot contain links or special files"
                    )


def matches(root, inventory):
    """Validate installed content, including edits or incomplete extraction."""
    return all(
        (root / name).is_file() and checksum(root / name) == digest
        for name, digest in inventory.items()
    )


def prepare_assets(assets):
    """Reuse ActivitySim archives and materialize inputs before suite preflight."""
    for asset in assets:
        cached, target = Path(asset["cache_file"]), Path(asset["destination"])
        cached.parent.mkdir(parents=True, exist_ok=True)
        with (cached.parent / (cached.name + ".abench.lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            download(asset, cached)
            if asset["unpack"]:
                storage = cached.parent / ".abench-extracted"
                storage.mkdir(exist_ok=True)
                extracted = storage / asset["sha256"]
                marker = storage / (asset["sha256"] + ".json")
                inventory = read_json(marker)
                if not inventory or not matches(extracted, inventory):
                    if extracted.exists():
                        raise ValueError(
                            f"extracted cache was modified: {extracted}; remove it to rebuild"
                        )
                    with tempfile.TemporaryDirectory(dir=storage) as temporary:
                        staging = Path(temporary) / "files"
                        staging.mkdir()
                        extract(cached, staging)
                        inventory = {
                            str(p.relative_to(staging)): checksum(p)
                            for p in staging.rglob("*")
                            if p.is_file()
                        }
                        if not inventory:
                            raise ValueError(
                                f"asset archive contains no files: {cached}"
                            )
                        staging.rename(extracted)
                        write_json(marker, inventory)
                if target.exists() or target.is_symlink():
                    if not target.is_dir() or not matches(target, inventory):
                        raise ValueError(
                            f"existing asset destination differs; refusing to overwrite: {target}"
                        )
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.symlink_to(extracted, target_is_directory=True)
            else:
                if target.exists() or target.is_symlink():
                    if not target.is_file() or checksum(target) != asset["sha256"]:
                        raise ValueError(
                            f"existing asset destination differs; refusing to overwrite: {target}"
                        )
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # File symlinks pointing outside /data break in Docker. Plain
                    # files are copied; whole unpacked directories can be linked
                    # because abench resolves data_dir before mounting it.
                    with tempfile.NamedTemporaryFile(
                        dir=target.parent, delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                    try:
                        shutil.copyfile(cached, temporary)
                        temporary.replace(target)
                    finally:
                        temporary.unlink(missing_ok=True)
            print(f"Asset ready: {target}", flush=True)

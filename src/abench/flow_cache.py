"""Persistent flow artifacts, isolated from measured runs and model data."""

import fcntl
import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .common import read_json, write_json


def cache_key(identity):
    """Use installed compiler identities rather than the entire experiment spec."""
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


@contextmanager
def reuse_flows(root, identity, destination):
    """Serialize compatible experiments and atomically publish successful snapshots.

    Keeping the lock through all attempts prevents concurrent writers from losing
    Numba signature indexes when a measured attempt compiles additional flows.
    copy2 preserves source mtimes, which Numba checks when loading compiled code.
    """
    bucket = Path(root).expanduser().resolve() / cache_key(identity)
    bucket.mkdir(parents=True, exist_ok=True)
    with (bucket / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = read_json(bucket / "current.json", {})
        previous = bucket / current["snapshot"] if current else None
        restored = 0
        if previous:
            restored = sum(path.is_file() for path in previous.rglob("*"))
            shutil.copytree(previous, destination, dirs_exist_ok=True)
        metadata = {
            "key": bucket.name,
            "directory": str(bucket),
            "restored_files": restored,
            "published": False,
        }
        yield metadata
        publish_flows(identity, destination, metadata)


def publish_flows(identity, destination, metadata):
    """Publish under the lock held by reuse_flows, including completed attempts."""
    bucket = Path(metadata["directory"])
    current = read_json(bucket / "current.json", {})
    previous = bucket / current["snapshot"] if current else None
    snapshot = Path(tempfile.mkdtemp(prefix="snapshot-", dir=bucket))
    try:
        if destination.exists():
            shutil.copytree(destination, snapshot, dirs_exist_ok=True)
        write_json(bucket / "identity.json", identity)
        pointer = bucket / "current.tmp"
        write_json(pointer, {"snapshot": snapshot.name})
        pointer.replace(bucket / "current.json")
    except BaseException:
        shutil.rmtree(snapshot)
        raise
    metadata["published"] = True
    if previous:
        shutil.rmtree(previous)

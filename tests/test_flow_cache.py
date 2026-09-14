"""Persistent reuse must preserve compiled artifacts and survive failed warmups."""

import os
from pathlib import Path

import pytest

from abench import cli
from abench.experiments import arguments
from abench.flow_cache import cache_key, reuse_flows


def test_reuse_accumulates_and_preserves_mtime(tmp_path):
    root = tmp_path / "shared"
    first, second, third = [tmp_path / name for name in ("first", "second", "third")]
    identity = {"sharrow": "abc", "numba": "1"}
    with reuse_flows(root, identity, first) as info:
        assert info["restored_files"] == 0
        first.mkdir()
        (first / "generated.py").write_text("code")
        os.utime(first / "generated.py", (1234567890, 1234567890))
        (first / "compiled.nbc").write_bytes(b"compiled")
    assert info["published"]
    with reuse_flows(root, identity, second) as info:
        assert info["restored_files"] == 2
        assert (second / "generated.py").stat().st_mtime_ns == (
            first / "generated.py"
        ).stat().st_mtime_ns
        (second / "additional.nbc").write_bytes(b"new signature")
    with reuse_flows(root, identity, third) as info:
        assert info["restored_files"] == 3
        assert (third / "compiled.nbc").read_bytes() == b"compiled"
    assert len(list((root / cache_key(identity)).glob("snapshot-*"))) == 1


def test_failed_warmup_does_not_publish(tmp_path):
    root = tmp_path / "shared"
    with reuse_flows(root, {}, tmp_path / "first"):
        (tmp_path / "first").mkdir()
        (tmp_path / "first/good").write_text("original")
    with pytest.raises(RuntimeError):
        with reuse_flows(root, {}, tmp_path / "failed"):
            (tmp_path / "failed/good").write_text("damaged")
            raise RuntimeError("warmup failed")
    with reuse_flows(root, {}, tmp_path / "last"):
        assert (tmp_path / "last/good").read_text() == "original"


def test_incompatible_environments_start_empty(tmp_path):
    with reuse_flows(tmp_path / "shared", {"numba": "1"}, tmp_path / "first"):
        (tmp_path / "first").mkdir()
        (tmp_path / "first/code").write_text("old")
    with reuse_flows(tmp_path / "shared", {"numba": "2"}, tmp_path / "second") as info:
        assert info["restored_files"] == 0
        assert not (tmp_path / "second").exists()


def test_cache_options_in_yaml(tmp_path):
    args = cli.parser().parse_args(
        arguments({"flow_cache_dir": "shared", "reuse_flows": False}, tmp_path)
    )
    assert args.flow_cache_dir == tmp_path / "shared"
    assert args.reuse_flows is False
    assert cli.parser().parse_args([]).reuse_flows is True
    assert (
        cli.parser().parse_args([]).flow_cache_dir
        == Path.home() / ".cache/abench/flows"
    )


def test_identity_tracks_compiler_sources_but_not_activitysim(tmp_path, monkeypatch):
    """Equal release versions must not hide a changed source commit."""
    import json

    from abench.runtime.cache_identity import identity

    monkeypatch.setattr("importlib.metadata.version", lambda name: "1.0")
    provenance = tmp_path / "sources.json"
    pins = [
        {
            "name": "sharrow",
            "repository": "ActivitySim/sharrow",
            "resolved_commit": "a",
        },
        {
            "name": "activitysim",
            "repository": "ActivitySim/activitysim",
            "resolved_commit": "b",
        },
    ]
    provenance.write_text(json.dumps(pins))
    original = identity(provenance)
    pins[1]["resolved_commit"] = "c"
    provenance.write_text(json.dumps(pins))
    assert cache_key(identity(provenance)) == cache_key(original)
    pins[0]["resolved_commit"] = "d"
    provenance.write_text(json.dumps(pins))
    assert cache_key(identity(provenance)) != cache_key(original)

"""Asset integrity, archive handling, and ActivitySim cache interoperability."""

import gzip
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest
import zstandard

from abench import assets


def plan(tmp_path, content, name="data.csv", unpack=None):
    info = dict(
        url="https://example.test/" + name, sha256=hashlib.sha256(content).hexdigest()
    )
    if unpack:
        info["unpack"] = unpack
    base = tmp_path / "model"
    base.mkdir(exist_ok=True)
    return assets.plan_assets(
        dict(cache_dir=str(tmp_path / "cache"), name="example", assets={name: info}),
        base,
    )


def mock_download(monkeypatch, data):
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        return io.BytesIO(data)

    monkeypatch.setattr(assets.urllib.request, "urlopen", fetch)
    return calls


def test_plain_download_and_reuse(tmp_path, monkeypatch):
    instructions = plan(tmp_path, b"id\n1\n")
    calls = mock_download(monkeypatch, b"id\n1\n")
    assets.prepare_assets(instructions)
    assets.prepare_assets(instructions)
    assert len(calls) == 1
    assert (tmp_path / "model/data.csv").read_bytes() == b"id\n1\n"
    assert not (tmp_path / "model/data.csv").is_symlink()


def test_gzip_checksum_is_decompressed(tmp_path, monkeypatch):
    instructions = plan(tmp_path, b"data")
    instructions[0]["url"] += ".gz"
    mock_download(monkeypatch, gzip.compress(b"data"))
    assets.prepare_assets(instructions)
    assert Path(instructions[0]["cache_file"]).read_bytes() == b"data"


def test_bad_download_preserves_cache(tmp_path, monkeypatch):
    instructions = plan(tmp_path, b"good")
    cached = Path(instructions[0]["cache_file"])
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"old")
    mock_download(monkeypatch, b"wrong")
    with pytest.raises(ValueError, match="checksum mismatch"):
        assets.prepare_assets(instructions)
    assert cached.read_bytes() == b"old"
    assert not Path(instructions[0]["destination"]).exists()


@pytest.mark.parametrize("suffix", [".zip", ".tar.gz", ".tar.zst"])
def test_unpack_reuses_verified_extraction(tmp_path, monkeypatch, suffix):
    buffer = io.BytesIO()
    if suffix == ".zip":
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("households.csv", "id\n1\n")
    else:
        with tarfile.open(fileobj=buffer, mode="w") as t:
            member = tarfile.TarInfo("households.csv")
            member.size = 5
            t.addfile(member, io.BytesIO(b"id\n1\n"))
        data = buffer.getvalue()
        buffer = io.BytesIO(
            gzip.compress(data)
            if suffix == ".tar.gz"
            else zstandard.ZstdCompressor().compress(data)
        )
    instructions = plan(tmp_path, buffer.getvalue(), "data" + suffix, "data_full")
    calls = mock_download(monkeypatch, buffer.getvalue())
    assets.prepare_assets(instructions)
    monkeypatch.setattr(
        assets, "extract", lambda *args: pytest.fail("unnecessary re-extraction")
    )
    assets.prepare_assets(instructions)
    assert len(calls) == 1
    target = Path(instructions[0]["destination"])
    assert target.is_symlink()
    assert (target.resolve() / "households.csv").read_text() == "id\n1\n"


def test_existing_data_is_not_overwritten(tmp_path, monkeypatch):
    instructions = plan(tmp_path, b"new")
    target = Path(instructions[0]["destination"])
    target.write_text("user data")
    mock_download(monkeypatch, b"new")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        assets.prepare_assets(instructions)
    assert target.read_text() == "user data"


def test_reject_archive_escape(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("../escape", "bad")
    instructions = plan(tmp_path, buffer.getvalue(), "data.zip", "data")
    mock_download(monkeypatch, buffer.getvalue())
    with pytest.raises(ValueError, match="relative"):
        assets.prepare_assets(instructions)
    assert not Path(instructions[0]["destination"]).exists()


def test_shared_with_activitysim_in_both_directions(tmp_path, monkeypatch):
    from activitysim.cli import create
    from activitysim.examples.external import default_cache_dir

    assert assets.plan_assets(
        dict(assets={"a": dict(url="https://example.test/a", sha256="a" * 64)}),
        tmp_path,
    )[0]["cache_file"] == str(default_cache_dir() / "a")
    instructions = plan(tmp_path, b"shared")
    item = instructions[0]
    cached = Path(item["cache_file"])
    cached.parent.mkdir(parents=True)
    # Simulate the file previously downloaded by ActivitySim. Its actual download
    # function must accept abench's file without any requests call, and vice versa.
    cached.write_bytes(b"shared")
    mock_download(monkeypatch, b"wrong")
    assets.prepare_assets(instructions)
    cached.unlink()
    Path(item["destination"]).unlink()
    mock_download(monkeypatch, b"shared")
    assets.prepare_assets(instructions)
    monkeypatch.setattr(
        create.requests,
        "get",
        lambda *a, **k: pytest.fail("ActivitySim redownloaded abench's cache"),
    )
    output = tmp_path / "activitysim-copy/data.csv"
    create.download_asset(
        item["url"],
        output,
        sha256=item["sha256"],
        link=cached.parent,
        base_path=output.parent,
    )
    assert output.read_bytes() == b"shared"


def test_suite_prepares_before_preflight_and_prepare_only_skips_runs(
    tmp_path, monkeypatch
):
    import yaml

    from abench.experiments import run_suite

    content = b"id\n1\n"
    path = tmp_path / "abench.yaml"
    path.write_text(
        yaml.safe_dump(
            dict(
                schema_version=1,
                output_root="results-${timestamp}",
                data_assets=dict(
                    cache_dir="cache",
                    assets={
                        "data/households.csv": dict(
                            url="https://example.test/households.csv",
                            sha256=hashlib.sha256(content).hexdigest(),
                        )
                    },
                ),
                runs={"example": {}},
            )
        )
    )
    calls = mock_download(monkeypatch, content)
    assert (
        run_suite(
            path,
            lambda argv: pytest.fail("prepare invoked model validation"),
            prepare_only=True,
        )
        == 0
    )
    assert len(calls) == 1
    assert not list(tmp_path.glob("results-*"))
    invoked = []

    def invoke(argv):
        assert (tmp_path / "data/households.csv").read_bytes() == content
        invoked.append(argv[0])
        return 0

    assert run_suite(path, invoke) == 0
    assert invoked == ["validate", "run"]
    assert len(calls) == 1


def test_validate_never_downloads(tmp_path, monkeypatch):
    import yaml

    from abench.experiments import run_suite

    path = tmp_path / "abench.yaml"
    path.write_text(
        yaml.safe_dump(
            dict(
                schema_version=1,
                output_root="results-${timestamp}",
                runs={"x": {}},
                data_assets=dict(
                    cache_dir="cache",
                    assets={
                        "data.csv": dict(
                            url="https://example.test/data", sha256="a" * 64
                        )
                    },
                ),
            )
        )
    )
    monkeypatch.setattr(
        assets.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("validate downloaded data"),
    )
    assert run_suite(path, lambda argv: 0, validate_only=True) == 0
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("filename", ["../outside", "/absolute", "foo/../../bad"])
def test_invalid_paths_rejected_before_download(tmp_path, filename):
    with pytest.raises(ValueError, match="relative"):
        assets.plan_assets(
            dict(
                assets={filename: dict(url="https://example.test/a", sha256="a" * 64)}
            ),
            tmp_path,
        )

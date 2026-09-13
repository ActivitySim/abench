"""Exercise source verification without network access or host package mutation."""

import json
import zipfile
from types import SimpleNamespace

import pytest

from abench.runtime import build_sources
from abench.sources import source

SHA = "a" * 40


def fake_commands(tmp_path, monkeypatch, *, resolved=SHA, metadata_name="addon"):
    """Emulate Git/pip boundaries but pass real wheel metadata through the builder."""
    calls = []

    def command(args):
        calls.append(args)
        if args[-2:] == ["rev-parse", "HEAD"]:
            return resolved
        if "wheel" in args:
            wheel_dir = tmp_path / "wheels/addon"
            wheel_dir.mkdir(parents=True)
            with zipfile.ZipFile(
                wheel_dir / "addon-1.0-py3-none-any.whl", "w"
            ) as archive:
                archive.writestr(
                    "addon-1.0.dist-info/METADATA",
                    f"Name: {metadata_name}\nVersion: 1.0\n",
                )
        return ""

    monkeypatch.setattr(build_sources, "command", command)
    monkeypatch.setattr(
        build_sources.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="1.0",
            read_text=lambda key: json.dumps(
                {"url": (tmp_path / "wheels/addon/addon-1.0-py3-none-any.whl").as_uri()}
            ),
        ),
    )
    return calls


def test_builder_verifies_and_installs_wheels_together(tmp_path, monkeypatch):
    calls = fake_commands(tmp_path, monkeypatch)
    item = source(f"addon[fast]=org/addon@{SHA}")
    build_sources.install(
        dict(sources=[item], requirements=["numpy"], constraints=["numpy<3"]), tmp_path
    )
    install = next(c for c in calls if "install" in c)
    assert any(value.endswith(".whl[fast]") for value in install)
    assert "numpy" in install
    assert (tmp_path / "constraints.txt").read_text() == "numpy<3\n"
    assert (
        json.loads((tmp_path / "source-provenance.json").read_text())[0][
            "resolved_commit"
        ]
        == SHA
    )


@pytest.mark.parametrize(
    "options,match",
    [
        ({"resolved": "b" * 40}, "Git commit mismatch"),
        ({"metadata_name": "different"}, "wrong distribution"),
    ],
)
def test_builder_rejects_wrong_identity_before_install(
    tmp_path, monkeypatch, options, match
):
    calls = fake_commands(tmp_path, monkeypatch, **options)
    with pytest.raises(ValueError, match=match):
        build_sources.install(
            dict(sources=[source(f"addon=org/addon@{SHA}")]), tmp_path
        )
    assert not any("install" in c for c in calls)

"""Contracts that let a new model or source dependency reuse the same harness."""

import json

import pytest
import yaml

from abench import cli
from abench.profiles import load_profile, validate_model
from abench.runtime.worker import make_state
from abench.sources import resolve_sources, source

SHA = "a" * 40
OTHER = "b" * 40


def test_source_overrides_and_alias_conflicts():
    defaults = [f"activitysim=ActivitySim/activitysim@{SHA}", f"My_Ext=org/old@{SHA}"]
    resolved = resolve_sources(
        defaults, [f"my-ext[fast]=org/new@{OTHER}#subdirectory=python/package"]
    )
    assert resolved[1] == dict(
        name="my-ext",
        repository="org/new",
        commit=OTHER,
        subdirectory="python/package",
        extras=["fast"],
    )
    with pytest.raises(ValueError, match="conflicting"):
        resolve_sources([], [f"activitysim=fork/activitysim@{SHA}"], SHA)
    with pytest.raises(ValueError, match="duplicate"):
        resolve_sources([], [f"activitysim=org/a@{SHA}"] * 2)


@pytest.mark.parametrize(
    "value",
    [
        "x=org/repo@main",
        f"x=org/repo@{SHA}#subdirectory=../bad",
        f"x=org/repo@{SHA}#subdirectory=/bad",
        f"x=org/repo;echo@{SHA}",
        dict(name="x", repository="org/repo", commit=SHA, extras=["x;echo"]),
    ],
)
def test_invalid_source_rejected(value):
    with pytest.raises(ValueError):
        source(value)


def make_model(root):
    """A self-contained profile is independent of either example repository."""
    (root / "configs").mkdir()
    (root / "data").mkdir()
    (root / "data/households.csv").write_text("household_id\n1\n2\n")
    (root / "configs/settings.yaml").write_text(
        "chunk_size: 100\nmodels: []\nmultiprocess_steps:\n- name: mp_households\n  begin: test_step\n  num_processes: 20\n  slice:\n    tables: [households]\n"
    )
    profile = dict(
        schema_version=1,
        name="tiny",
        configs=["configs"],
        snapshot=["configs"],
        settings={"chunk_size": 200},
        required_inputs=["households.csv"],
        sources=[f"activitysim=ActivitySim/activitysim@{SHA}"],
    )
    (root / "benchmark.yaml").write_text(yaml.safe_dump(profile))
    return profile


def test_profiles_preflight_and_oversampling(tmp_path):
    make_model(tmp_path)
    profile = load_profile("benchmark.yaml", tmp_path)
    validate_model(profile, tmp_path, tmp_path / "data", 2)
    with pytest.raises(ValueError, match="only 2"):
        validate_model(profile, tmp_path, tmp_path / "data", 3)
    (tmp_path / "data/households.csv").unlink()
    with pytest.raises(ValueError, match="missing required"):
        validate_model(profile, tmp_path, tmp_path / "data", 0)
    for name in ("mtc", "mtc-extended", "sandag"):
        assert load_profile(name, tmp_path)["configs"]
    extended = load_profile("mtc-extended", tmp_path)
    assert extended["configs"] == ["ext-configs", "configs"]
    assert extended["mp_configs"] == ["ext-configs_mp"]
    assert extended["models_from"] == "ext-configs/settings.yaml"


def test_component_summary_profile_contract(tmp_path):
    profile = make_model(tmp_path)
    profile["component_summaries"] = {
        "custom_*": {
            "table": "trips",
            "outcomes": ["mode"],
            "segments": ["purpose"],
            "filters": {"modeled": True},
        },
        "diagnostic_*": False,
    }
    profile["component_summary_category_limit"] = 25
    path = tmp_path / "benchmark.yaml"
    path.write_text(yaml.safe_dump(profile))
    loaded = load_profile("benchmark.yaml", tmp_path)
    assert loaded["component_summary_category_limit"] == 25

    profile["component_summaries"]["custom_*"]["outcomes"] = "mode"
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(ValueError, match="outcomes"):
        load_profile("benchmark.yaml", tmp_path)


def test_config_overlay_and_process_precedence(tmp_path):
    profile = make_model(tmp_path)
    overlay = tmp_path / "overlay-0"
    overlay.mkdir()
    (overlay / "settings.yaml").write_text(
        "inherit_settings: true\nchunk_size: 300\nchunk_training_mode: explicit\nnum_processes: 99\n"
    )
    phase = tmp_path / "measured"
    phase.mkdir()
    (phase / "output").mkdir()
    spec = dict(
        profile=profile,
        households=2,
        multiprocess=True,
        processes=4,
        sharrow=False,
        use_explicit_error_terms=False,
        config_overlay=[str(overlay)],
    )
    state = make_state(spec, phase, tmp_path, tmp_path / "data", tmp_path)
    assert state.settings.chunk_size == 300
    assert state.settings.chunk_training_mode == "explicit"
    assert state.settings.num_processes == 4
    assert state.settings.multiprocess_steps[0].num_processes == 4
    assert "chunk_size: 100" in (tmp_path / "configs/settings.yaml").read_text()


def test_explicit_error_terms_biases_location_choice_logsums(tmp_path):
    profile = make_model(tmp_path)
    phase = tmp_path / "measured"
    phase.mkdir()
    (phase / "output").mkdir()
    spec = dict(
        profile=profile,
        households=2,
        multiprocess=False,
        processes=1,
        sharrow=False,
        use_explicit_error_terms=True,
        config_overlay=[],
    )
    state = make_state(spec, phase, tmp_path, tmp_path / "data", tmp_path)
    assert state.settings.use_explicit_error_terms is True
    assert state.settings.bias_location_choice_logsums_for_poisson_sampling is True


def test_validate_does_not_build_or_write(tmp_path, monkeypatch, capsys):
    make_model(tmp_path)
    calls = []

    def command(args, log=None):
        calls.append(args)
        return json.dumps(dict(OSType="linux", CgroupVersion="2"))

    monkeypatch.setattr(cli, "command", command)
    assert (
        cli.main(
            [
                "validate",
                "--model-dir",
                str(tmp_path),
                "--no-sharrow",
                "--households",
                "2",
            ]
        )
        == 0
    )
    assert calls == [["docker", "info", "--format", "{{json .}}"]]
    assert json.loads(capsys.readouterr().out)["sources"][0]["commit"] == SHA
    assert not (tmp_path / "measured").exists()


def test_cli_failure_keeps_report(tmp_path, monkeypatch):
    make_model(tmp_path)
    output = tmp_path / "experiment"

    def command(args, log=None):
        if args[:2] == ["docker", "info"]:
            return json.dumps(dict(OSType="linux", CgroupVersion="2"))
        raise RuntimeError("deliberate build failure")

    monkeypatch.setattr(cli, "command", command)
    with pytest.raises(RuntimeError, match="deliberate"):
        cli.main(
            [
                "run",
                "--model-dir",
                str(tmp_path),
                "--no-sharrow",
                "--households",
                "2",
                "--output-dir",
                str(output),
            ]
        )
    assert (output / "report.html").is_file()
    assert (
        json.loads((output / "experiment.json").read_text())["failure"]["phase"]
        == "build"
    )
    assert (output / "runner/build_sources.py").is_file()

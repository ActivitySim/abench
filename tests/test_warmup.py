"""Cache preparation is small and serial without relaxing measurement validity."""

import copy

import pandas as pd
import pytest

from abench.cli import parser
from abench.runtime.worker import phase_spec


@pytest.mark.parametrize(
    "target,cap,expected",
    [(28365, 500, 500), (200, 500, 200), (500, 500, 500), (28365, 1000, 1000)],
)
def test_warmup_cap_and_measured_isolation(target, cap, expected):
    spec = dict(
        households=target,
        warmup_households=cap,
        multiprocess=True,
        processes=4,
        profile={"configs": ["configs"]},
    )
    original = copy.deepcopy(spec)
    warmup = phase_spec(spec, "warmup")
    assert warmup["households"] == expected
    assert not warmup["multiprocess"]
    assert warmup["processes"] == 1
    assert warmup["_target_multiprocess"]
    assert phase_spec(spec, "measured") == original
    assert spec == original


@pytest.mark.parametrize("count", [3, 600])
@pytest.mark.parametrize("format", ["csv", "parquet"])
def test_full_population_caps_available_households(tmp_path, count, format):
    data = pd.DataFrame({"household_id": range(count)})
    if format == "csv":
        data.to_csv(tmp_path / "households.csv", index=False)
    else:
        data.to_parquet(tmp_path / "households.parquet", index=False)
    spec = dict(households=0, multiprocess=True, processes=4, profile={})
    assert phase_spec(spec, "warmup", tmp_path)["households"] == min(count, 500)
    assert phase_spec(spec, "measured", tmp_path)["households"] == 0


def test_custom_household_table_and_empty_population(tmp_path):
    spec = dict(
        households=0,
        multiprocess=False,
        processes=1,
        profile={"household_table": "population.csv"},
    )
    (tmp_path / "population.csv").write_text("id\n")
    with pytest.raises(ValueError, match="empty household"):
        phase_spec(spec, "warmup", tmp_path)
    (tmp_path / "population.csv").write_text("id\n1\n2\n")
    assert phase_spec(spec, "warmup", tmp_path)["households"] == 2


def test_default_and_override_parser():
    assert parser().parse_args([]).warmup_households == 500
    assert parser().parse_args(["--warmup-households", "100"]).warmup_households == 100

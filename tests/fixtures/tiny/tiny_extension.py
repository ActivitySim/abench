"""Tiny real ActivitySim workflow: exercises registration, slicing, and caching."""

import importlib
import sys
from pathlib import Path

import pandas as pd
from activitysim.core import workflow


@workflow.step
def bench_initialize(state: workflow.State):
    """Seed four rows and a generated Numba module in the guarded flow directory."""
    households = pd.read_csv(Path("/data/households.csv"), index_col="household_id")
    if state.settings.households_sample_size:
        households = households.head(state.settings.households_sample_size)
    state.add_table("households", households)
    cache = Path(state.settings.sharrow_cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    module = cache / "tiny_generated.py"
    if not module.exists():
        module.write_text(
            "from numba import njit\n@njit(cache=True)\ndef twice(x):\n    return x * 2\n"
        )


@workflow.step
def bench_compute(state: workflow.State, households: pd.DataFrame):
    """Run independently in each slice; measured cache misses must be rejected."""
    sys.path.insert(0, state.settings.sharrow_cache_dir)
    twice = importlib.import_module("tiny_generated").twice
    households = households.copy()
    households["value"] = [twice(int(value)) for value in households["value"]]
    state.add_table("households", households)


@workflow.step
def bench_export(state: workflow.State, households: pd.DataFrame):
    """Export merged results so the harness validates the realized population."""
    households.to_csv(Path(state.filesystem.output_dir) / "final_households.csv")


def prepare(state, spec, phase):
    """Supply tiny shared skims; this fixture has no destination/shadow models."""
    state.set("shadow_pricing_info", None)
    state.set("shadow_pricing_choice_info", None)
    state.set("network_los_preload", None)
    if spec["sharrow"]:
        import numpy as np
        import sharrow as sh

        dataset = sh.Dataset({"distance": (("otaz", "dtaz"), np.ones((1, 1)))})
        state.set(
            "skim_dataset",
            dataset.shm.to_shared_memory("skim_dataset", mode="r", load=True),
        )

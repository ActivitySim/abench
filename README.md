# abench

Run reproducible ActivitySim runtime and memory experiments in Linux Docker,
from macOS or Linux. One runner supports MTC, SANDAG ABM3, and other models through
small YAML profiles. ActivitySim itself does not need to be installed on the host.

```bash
python -m pip install /path/to/abench
abench run --model-dir /path/to/sandag-abm3-example --profile sandag \
  --source activitysim=ActivitySim/activitysim@<full-40-character-SHA> \
  --source sharrow=ActivitySim/sharrow@<full-40-character-SHA> \
  --multiprocess --processes 4 --sharrow --households 28365 \
  --memory 32g --shm-size 8g --output-dir /path/to/experiments/sandag
```

Use `--profile mtc` for MTC; both profiles ship with the package. SANDAG defaults
to its small `benchmarking-data`, **not full-scale skims**. MTC defaults to
`data_full`. `--data-dir` overrides either. Model directories need not be Git
repositories; Git revision/status are recorded where available and model files
are always snapshotted. Existing example scripts and normal configs are untouched.

The host needs Python 3.10+, PyYAML (installed with abench), and a Linux Docker
engine with cgroup v2 and `memory.peak`. Docker Desktop must have enough VM RAM
for the chosen memory limit plus VM overhead. The default container is Debian
Bookworm/Python 3.11. Current instrumentation requires ActivitySim's
`workflow.State` API (1.4-era or newer); arbitrary historical revisions are not
promised to work. Build/runtime failures retain diagnostics and a failure report.

## Named experiment files

Write common options once and override only what differs between runs:

```yaml
schema_version: 1
vars:
  households: 28365
  warmup_households: 5000
  model: /path/to/sandag-abm3-example
output_root: ./results/sandag-${timestamp}
defaults:
  model_dir: ${model}
  profile: sandag
  data_dir: ${model}/benchmarking-data
  config_overlay: ["${model}/configs_explicit_chunk"]
  multiprocess: true
  processes: 4
  sharrow: true
  households: ${households}
  warmup_households: ${warmup_households}
  memory: 80g
  shm_size: 8g
  sources:
    - sharrow=ActivitySim/sharrow@fc175b27d8e0c5d202721c67d96b050e6117b235
runs:
  main:
    sources:
      - activitysim=ActivitySim/activitysim@5c6fae24a91a57a2d6dfc2e1dbe062a61d94545a
  pr1110:
    sources:
      - activitysim=ActivitySim/activitysim@51e298a84276813946e1d623c9a5785e078e022f
```

Save it as `sandag.yaml`, then run:

```bash
abench sandag.yaml
# Or check all runs without building images or running models:
abench validate sandag.yaml
```

A ready-to-use [SANDAG chunked suite](examples/sandag-chunked.yaml) is included
in the repository. Its paths assume abench and the SANDAG repository are siblings.
The pinned `main` revision is the one used in the earlier trials, not a moving
branch reference.

- `defaults` accepts CLI options using underscores (`shm_size`, `config_overlay`,
  etc.). Use `multiprocess: false` for serial execution and `sharrow: false` to
  disable Sharrow. `sources` accepts the same strings/mappings as model profiles.
- `runs` is an ordered mapping of names to overrides. Each run inherits defaults;
  ordinary values and lists are replaced. **Sources merge by normalized package
  name**, so changing ActivitySim does not discard the shared Sharrow pin.
- `${name}` substitutes a reusable scalar from `vars`; terms can reference other
  terms. A whole-value reference preserves its type, including numbers/booleans.
  Undefined references and cycles are errors. No shell or environment expansion
  is performed. `${timestamp}` is a built-in UTC launch identifier shared by all
  runs, with microseconds to avoid reusing output directories.
- All explicit paths in the suite are relative to the YAML file, independent of
  the terminal's current directory. This includes overlays and custom profile
  paths. Built-in `mtc`/`sandag` profile names retain their meaning. When omitted,
  `model_dir` defaults to the YAML file's directory; the model profile still
  supplies its usual default data/config paths.
- The suite owns output locations: `output_root/<run-name>/`. Set `output_root`
  once instead of `output_dir` in each run. Existing roots are rejected.
- All runs are preflighted before the first starts, then run sequentially in file
  order. Failure stops the suite and retains partial results. The combined report
  is `output_root/comparison.html`; individual runs retain their own reports.
  `experiments.yaml` and `suite.json` record the original file and expanded plan.
- File invocations do not accept additional CLI overrides. Edit `defaults` or the
  relevant run to keep the file a complete description of the experiment.

This experiment file describes **which tests to run**. A model profile such as
`benchmark.yaml` describes **how to configure a model**, and remains reusable
across suites.

## Run controls

- `--single-process` (default), or `--multiprocess --processes N`. The count applies
  to every sliced stage; coordinators are additional processes.
- `--sharrow` (default) or `--no-sharrow`. Sharrow enabled requires its source pin.
- `--households N` (default 1,000); zero uses the original full input population.
  abench never replicates households. Positive samples must match realized output.
- `--config-overlay configs_explicit_chunk` adds config directories in listed
  priority order. Relative overlay paths are relative to the model directory.
- `--memory 16g`, `--shm-size 8g`, `--interval 0.5`, and optional `--platform`.
- `--output-dir` must be new. `--label` names an experiment, and `--compare` accepts
  earlier experiment directories. Compatible compiled flows are reused automatically;
  the serial warmup still runs.

`abench validate` accepts the same experiment arguments without `--output-dir`.
It checks the profile, required inputs, CSV population size, source pin syntax,
and Docker capabilities without building an image or running the model. It does
not prove Git commit availability, package compatibility, or skim consistency;
those are checked by the build and model run.

## Any dependency from GitHub source

Repeat `--source` for any Python distribution, including add-on extensions:

```bash
--source 'my-addon[fast]=ExampleOrg/model-addon@<SHA>#subdirectory=python/addon'
```

The left side is the **distribution name** (which can differ from its import
module). Each source uses an exact full SHA and an `organization/repository` name.
Extras and a repository subdirectory are optional. Profiles can declare the same
entries as strings or mappings:

```yaml
sources:
  - name: my-addon
    repository: ExampleOrg/model-addon
    commit: '0123456789abcdef0123456789abcdef01234567'
    extras: [fast]
    subdirectory: python/addon
```

CLI sources override profile sources by normalized distribution name. Duplicate
CLI entries are errors. `--activitysim-commit` and `--sharrow-commit` remain aliases
for the official repositories; conflicting alias/source declarations are errors.

Inside Docker, abench verifies each checkout's Git object, builds a wheel, checks
its distribution name, then installs all source wheels together with other
requirements. It runs `pip check` and verifies installed wheel identities. Exact
source commits, resolved versions, and the full dependency environment are saved.
All source dependencies must agree: conflicting requirements fail the build.

Use profile `requirements` for additional registry requirements and `constraints`
for resolver bounds or exact transitive pins. Profiles may select `python_image`
(a compatible Debian-based image, optionally pinned by digest). The default image
includes a compiler and HDF5 headers. Packages requiring other system libraries
can use a prebuilt compatible base image. Private GitHub authentication and custom
OS provisioning are outside the initial interface.

Source pins do not freeze unpinned transitive/build dependencies or base images.
Retain images and dependency manifests for strict reproduction. Build isolation
may fetch build requirements; constraints currently govern the final environment,
not those isolated build environments.

## Add another model

Create `benchmark.yaml` in its model directory, then use `--model-dir`:

```yaml
schema_version: 1
name: My regional model
configs: [configs]
mp_configs: [configs_mp]
snapshot: [configs, configs_mp, extensions]
extensions: [extensions]
data_dir: data
required_inputs:
  - [households.csv, households.parquet]
  - persons.csv
  - land_use.csv
  - skims.omx
settings:
  use_shadow_pricing: false
  rng_base_seed: 0
input_tables:
  households: {}
  persons: {}
  land_use:
    totals: [TOTPOP, TOTHH, TOTEMP]
    zone_columns: [TAZ]
output_tables:
  households: {}
  persons: {}
  tours: {}
  trips:
    categories: [trip_mode, primary_purpose]
```

Paths in a profile are relative to the model directory (except `data_dir`, which
may be absolute). `configs` and `mp_configs` are ordered, highest priority first.
`snapshot` must cover config files, local extension modules, and adapter modules;
entries must not overlap or contain directory symlinks. Data is mounted read-only
and should not change during a run. Profiles are trusted model code/configuration.

Settings precedence, lowest to highest: **normal model configs → profile settings
→ user overlays → required CLI controls** (sample, SP/MP, worker counts, Sharrow,
and fail-fast). Generated inheriting profile configs preserve this ordering when
ActivitySim reconstructs worker settings. Component overlays remain independent.

Optional profile fields:

| Field | Purpose |
|---|---|
| `models_from`, `exclude_models` | Take a YAML `models` list and explicitly omit diagnostic steps. Otherwise use normal settings. |
| `mp_settings` | Read `multiprocess_steps` from a separate YAML file. |
| `extensions` | Import modules through ActivitySim's registration mechanism, including spawned workers. Installing a package alone does not register its components. |
| `adapter: module:function` | Optional `function(state, spec, phase)` initialization hook, called once in the model parent before execution. Use `state.import_extensions` for worker setup; parent-only mutations are not automatically worker initialization. |
| `input_tables`, `output_tables` | Logical table names mapped to summary options: `file` (stem), `totals`, `categories`, and `zone_columns` for land use. CSV and Parquet are supported. Use logical `households` for sample validation. |
| `output_prefix` | Default `final_`; applied to output file stems. |
| `household_table` | Default `households.csv`, used for early CSV sample validation. Parquet samples are checked after execution. |
| `zone_label` | Display label for land-use rows, such as zones or MAZs. |

For specialized data formats, an adapter can arrange compatible CSV/Parquet
summary outputs. The initial generic reader does not interpret arbitrary binary
model outputs.

## Measurement and reports

Sharrow runs first execute the model in a separate **single-process** warmup,
using **min(target households, 5000)** households by default. For `--households 0`,
the target is the full available population, so warmup uses at most 5000 of those
households. Set `--warmup-households N` (or `warmup_households: N` in experiment
YAML) to change this positive cap. Warmup always uses one process; measured runs
retain their requested sample and worker count. Model config directories, seed,
chunk overlays, and flow cache path are retained from the target experiment.

Compiled flows are automatically reused across runs and suites, including changes
in ActivitySim revisions, sample sizes, process counts, and model configs. The
persistent host cache defaults to `~/.cache/abench/flows`. Change it with
`--flow-cache-dir PATH` (`flow_cache_dir` in YAML), or disable automatic reads and
writes with `--no-reuse-flows` (`reuse_flows: false`). `--cache-from` remains an
explicit seed option with its existing stricter dependency checks.

Compatibility uses the **installed** Sharrow, Numba, llvmlite, and NumPy versions,
plus their source repository/commit identities when applicable, Python version,
and container architecture/CPU features. ActivitySim and model settings are
excluded from this key: Sharrow identifies generated flows by their contents,
and Numba checks cached signatures. Changed flows can compile during warmup.
An existing cache does **not** guarantee that warmup will need no compilation.

Each experiment receives a private copy with source timestamps preserved. Warmup
always runs, and successful warmups atomically update the persistent cache before
measurement starts. Compatible simultaneous warmups wait for each other to avoid
losing compiled signatures; measured runs remain independent. Only `cache/flows`
is shared, never model data, outputs, or shared-memory artifacts. Cache identity
and reuse counts are recorded in `flow-cache-identity.json` and `experiment.json`.
The persistent cache can be deleted between runs to reclaim disk space; older
experiments created before this feature are not automatically imported.

A smaller serial warmup may not exercise every flow signature required by the
measured run. **Cache misses still invalidate the measured experiment**: abench
does not silently compile, retry with compilation enabled, or accept incomplete
cache coverage. Increase the warmup cap when needed.
The CLI and failure report identify the phase, failed component, cache flow, and
log path; `cache-miss-details-*.jsonl` records the exact required Numba signature.
`phase-settings.json` and
`effective-settings.json` preserve the actual settings for each phase, and the
report includes the cache-build settings separately from the measured target.

Numba compilation of generated flow overloads is rejected during measurement; ordinary non-flow
compilation and disk-cache loading remain included. This guard uses private
Numba internals and is covered by real disk-cache hit/miss tests.

Memory is the whole-container cgroup v2 charge, counting shared pages once.
Blue is `memory.current` (including file cache, shared memory, and kernel costs).
Green dashed is `anon + shmem`, a subset excluding ordinary file cache and kernel
costs. Never add the lines. Swap is recorded separately and disabled by equal
memory/memory+swap limits. `/dev/shm` capacity is within that limit.

Component runtimes show worker mean, population SD, count, and maximum. Worker SD
is imbalance, not confidence across repeated trials. The component dropdown
highlights each worker's actual window; overlaps darken and gaps remain clear.
Overall elapsed includes startup, coordination, and checkpoint writes between
components. Kernel peak includes startup; warmup and post-run summaries are
excluded. Shared VM page-cache ownership can influence container charges: this
is not a cold-input I/O benchmark.

```bash
abench report --compare /path/to/run-a /path/to/run-b \
  --output-dir /path/to/comparison.html
```

Reports are offline HTML/SVG/JavaScript plus normalized JSON. They retain fastest
successful component highlighting and use common memory axes. Existing MTC and
SANDAG schema-1 experiments remain readable, including approximate legacy timing
windows where only completion logs exist. New experiments use schema 2 and include
source manifests, resolved profile/settings, model and harness snapshots, file
hashes, Docker details, and source installation provenance. Input file size/mtime
records are provenance hints, not content hashes of large skims.

## Development and CI

```bash
python -m pip install -e '.[test]' -r tests/requirements.txt
python -m pytest -m 'not docker'
pre-commit run --all-files
python -m build
ABENCH_DOCKER_TESTS=1 python -m pytest tests/test_docker.py -v
```

Install Node.js to exercise the offline chart selector test. GitHub Actions runs
unit tests on Python 3.10–3.12, lint/format checks, wheel packaging checks, and Linux
Docker integration. The Docker tests build pinned ActivitySim/Sharrow sources and
run a four-household extension workflow in serial and multiprocess modes, checking
warmup, generated-code cache reuse, per-worker timings, merged outputs, memory,
and reports. They do not require either example repository or large datasets.

Adapted from the MTC and SANDAG benchmark harnesses developed in this workspace.
The original measurement approach was informed by WSP's Lighthouse production
benchmark. See LICENSE for the retained BSD license.

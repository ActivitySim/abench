# abench

Run reproducible ActivitySim runtime and memory experiments in Linux Docker,
from macOS or Linux. One runner supports MTC, SANDAG ABM3, and other models through
small YAML profiles. ActivitySim itself does not need to be installed on the host.

With [uv](https://docs.astral.sh/uv/), run without managing a Python environment:

```bash
uvx abench --help
uvx abench experiments.yaml
```

For a specific release use `uvx abench@0.1.0 experiments.yaml`; use
`uvx abench@latest` to refresh to the latest release. Docker and model data must
still be available locally. macOS and Linux hosts are supported.

Alternatively, install with pip:

```bash
python -m pip install abench
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
inputs:
  households:
    type: integer
    default: 28365
    minimum: 0
  warmup_households:
    type: integer
    default: 5000
    minimum: 1
vars:
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

To compare **current main against a PR**, named suites also accept source mappings
with `branch` or `pr` in place of `commit` (local development feature, not in
PyPI 0.1.0 yet):

```yaml
schema_version: 1
inputs:
  activitysim_pr:
    type: integer
    required: true
    minimum: 1
    description: ActivitySim PR number to compare against current main
output_root: benchmark-runs/mtc-${timestamp}
defaults:
  profile: mtc
  households: 500000
  multiprocess: true
  processes: 4
  sharrow: true
  sources:
    - sharrow=ActivitySim/sharrow@fc175b27d8e0c5d202721c67d96b050e6117b235
runs:
  main:
    sources:
      - name: activitysim
        repository: ActivitySim/activitysim
        branch: main
  pr:
    label: ActivitySim PR ${activitysim_pr}
    sources:
      - name: activitysim
        repository: ActivitySim/activitysim
        pr: ${activitysim_pr}
```

Each mapping must provide exactly one of `commit`, `branch`, or `pr`. Branch/PR
selectors work for any source package in a suite, including extras/subdirectories.
Abench uses host Git and network access to resolve each selector once per suite,
before running models, and passes only exact commits to the builds. `pr` selects
`refs/pull/<number>/head` in the named repository, including PRs from forks;
it does **not** select the synthetic merge commit. Use a positive integer PR number.
`branch` names the branch without `refs/heads/`.

The resolved commits are printed and recorded in `suite.json` alongside the
original YAML, and in each experiment's source provenance. A new invocation
resolves the selectors again, including `validate` and `prepare`. To reproduce an
earlier suite, replace selectors with `commit: <recorded-full-SHA>`. CLI overrides
and model profiles still require exact commits.

Start the suite and answer its input prompts:

```bash
uvx abench ./activitysim-prototype-mtc/abench.yaml
# Preflight the same selection without running benchmarks:
uvx abench validate ./activitysim-prototype-mtc/abench.yaml
```

Declare user-facing settings in `inputs`, and keep internal reusable values and
expressions in `vars`. Only inputs can be overridden. For example:

```yaml
inputs:
  households:
    type: integer
    default: 500000
    minimum: 0
    description: Households to sample; 0 uses the full population
  mode:
    type: string
    default: chunked
    choices: [chunked, unchunked]
  sharrow:
    type: boolean
    default: true
vars:
  label: "${mode}, ${households} households"
```

In a terminal, run, validate, and prepare prompt for each input in YAML order.
Press Enter to accept a displayed default. Required inputs have no default and
must be entered; empty or invalid answers prompt again with an explanation.
Ctrl-C cancels before source resolution or downloads. Selected values are printed
and saved in `suite.json` as `input_values`.

For scripting, `--non-interactive` uses defaults and supplied values without
prompting. Non-terminal stdin behaves the same way; missing required inputs fail
instead of hanging. Repeatable `--set NAME=VALUE` can supply values explicitly;
these inputs are not prompted. Quote
arguments containing spaces, for example `--set 'label=My experiment'` if `label`
is declared as a string input. `abench experiment.yaml --help` lists the file's
inputs, descriptions, defaults, and constraints without requiring input values,
contacting GitHub, downloading data, or creating outputs.

- Every input requires a `type`: `string`, `integer`, `number`, or `boolean`.
  Give it either a typed YAML `default` or `required: true`, but not both.
- Optional `choices` restricts allowed values; `minimum`/`maximum` are inclusive
  bounds for numeric inputs. Numbers must be finite. Boolean overrides accept
  `true` or `false` (case insensitive), not `yes`, `no`, `1`, or `0`.
- Values are converted according to their declared type, never parsed as YAML.
  Strings retain literal text, including `=`, spaces, and `${...}`. Defaults and
  input declarations are literal too; use `vars` for derived expressions.
- Defaults are applied first, followed by command-line overrides, then `${...}`
  expansion. Inputs and vars share one namespace; duplicate names and the
  reserved name `timestamp` are errors. Input names use letters, digits, and
  underscores and cannot start with a digit.
- Unknown inputs, duplicate overrides, and invalid explicit values fail before
  source resolution or downloads. Missing required inputs prompt in a terminal
  and fail in non-interactive mode. There is no overriding vars.
- The YAML file is unchanged. `suite.json` records typed `cli_overrides`, effective
  `input_values`, the expanded configuration, and exact source resolutions.
  `experiments.yaml` preserves the original file, so replay its recorded overrides
  too when reproducing a run.

`branch: main` is freshly resolved on **every launch**; there is no saved branch
SHA to update manually. For the MTC example, enter the PR number when prompted,
then press Enter twice to accept 500,000 households and 4 processes.

These commands require a release containing this feature. Until then, use
`uvx --from /path/to/abench abench ...` with this local checkout.

- `defaults` accepts CLI options using underscores (`shm_size`, `config_overlay`,
  etc.). Use `multiprocess: false` for serial execution and `sharrow: false` to
  disable Sharrow. `sources` accepts the same strings/mappings as model profiles.
- `runs` is an ordered mapping of names to overrides. Each run inherits defaults;
  ordinary values and lists are replaced. **Sources merge by normalized package
  name**, so changing ActivitySim does not discard the shared Sharrow pin.
- `${name}` substitutes a scalar from `inputs` or `vars`; vars can reference
  other vars and inputs. A whole-value reference preserves its type, including numbers/booleans.
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
- File invocations accept `--set` and `--non-interactive`; other model options belong
  in `defaults` or the relevant run.

This experiment file describes **which tests to run**. A model profile such as
`benchmark.yaml` describes **how to configure a model**, and remains reusable
across suites.

Terminal progress identifies the experiment number, image build, warmup, and
measured attempts/retries. Builds and model phases print elapsed time every
15 seconds; model phases also show current and peak cgroup memory when samples
are available. Full console output stays in the printed log paths. These are
status updates, not an estimated completion percentage.

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
measurement starts. Compatible simultaneous experiments wait for each other to avoid losing
compiled signatures when attempts update Numba cache indexes. Only `cache/flows`
is shared, never model data, outputs, or shared-memory artifacts. Cache identity
and reuse counts are recorded in `flow-cache-identity.json` and `experiment.json`.
The persistent cache can be deleted between runs to reclaim disk space; older
experiments created before this feature are not automatically imported.

A smaller serial warmup may not exercise every flow/type signature needed by the
measured run. Each measured attempt therefore records flow compilation and allows
it to finish. If compilation occurred, the completed attempt becomes **cache
preparation**, and none of its runtime or memory results qualify as benchmark
results. Its diagnostics and outputs are retained under `attempts/attempt-001`,
`attempts/attempt-002`, etc. Newly compiled flows are published to the shared cache.

The model then restarts in a fresh container with fresh outputs and model caches,
using the same settings and expanded flow cache. Only an attempt with **zero flow
compilations** is accepted. All attempts keep permanent directories under
`attempts/`; `measured/` links to the accepted attempt. By default abench allows
**two additional attempts** (three total). Set `--cache-retries N` or
`cache_retries: N` in YAML; zero allows no retries. If compilation persists, the
experiment fails with the final attempt's diagnostics retained. Ordinary model
errors, OOMs, and output validation failures stop immediately and are never
retried as cache preparation.

The report and `experiment.json` include attempt history. Each attempt retains
`cache-miss-details-*.jsonl`, its settings, component timings, and memory samples.
Ordinary non-flow compilation and disk-cache loading remain included in accepted
measurements. Flow tracking uses private Numba internals and is covered by real
compilation, cache-hit, and Docker retry tests.

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

## Releases

See [RELEASING.md](https://github.com/ActivitySim/abench/blob/main/RELEASING.md)
for Trusted Publishing setup and release instructions.

## Downloading model data

Experiment suites may declare `data_assets` using ActivitySim's external-example
asset names, URLs, SHA-256 checksums, and `unpack` destinations:

```yaml
data_assets:
  name: prototype_mtc_extended
  assets:
    data_full.tar.zst:
      url: https://github.com/ActivitySim/activitysim-prototype-mtc/releases/download/v1.3.4/data_full.tar.zst
      sha256: b402506a61055e2d38621416dd9a5c7e3cf7517c0a9ae5869f6d760c03284ef3
      unpack: data_full
```

`abench experiment.yaml` prepares these assets before validating model inputs or
starting Docker. `abench prepare experiment.yaml` only prepares data. `abench
validate experiment.yaml` remains read-only: it checks declarations and requires
inputs to be present (use `prepare` first for a fresh clone). Asset destinations
are relative to the experiment YAML, regardless of `model_dir`; point each run's
`data_dir` to the appropriate destination. Variable substitutions also work here.

The default download cache is exactly
`platformdirs.user_cache_dir("ActivitySim")/External-Examples/<name>/`—the same
layout used by `activitysim.examples.external.download_external_example`.
`name` is optional; `cache_dir` can override the root before appending `name`.
Keep names and checksums identical to ActivitySim's declarations to share files.
For assets cached by direct `download_asset(link=True)` calls, set `cache_dir`
to that call's `platformdirs.user_data_dir("ActivitySim")` and omit `name`.
No host ActivitySim installation is needed.

Checksums are required and verified before reuse. For a `.gz` URL whose asset name
omits `.gz`, the checksum covers the decompressed file, as in ActivitySim.
Archives (`.tar.zst`, `.tar.gz`, `.zip`) use the archive checksum and preserve their
internal paths when unpacking. Verified extracted contents are cached under
`.abench-extracted/<sha256>/` beside the archive. Whole unpacked directories are
linked to the suite, and abench resolves `data_dir` before Docker mounts it.
Individual files are copied so external file symlinks cannot break inside Docker.
Existing destinations must match; modified inputs are never silently replaced.
Archive links, special files, and paths escaping the destination are rejected.
The original instructions and resolved cache locations are saved in `suite.json`.

This feature is not present in abench 0.1.0. Until the next release, install the
updated checkout (`uv tool install /path/to/abench`) or run it with
`uvx --from /path/to/abench abench /path/to/model/abench.yaml`.

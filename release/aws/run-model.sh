#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Run one full-scale ActivitySim release benchmark on a prepared EC2 host.

Required:
  --model mtc-extended|sandag
  --model-commit SHA
  --artifact-uri S3_URI
  --activitysim-commit SHA

Optional:
  --sharrow / --no-sharrow   default: --sharrow
  --sharrow-commit SHA       required only with --sharrow
  --eet / --no-eet           use_explicit_error_terms; default: --no-eet
  --smoke-test               skip model data and verify AWS host plumbing
  --work-dir PATH             default: /work
  --processes N               default: 16
  --memory LIMIT              default: 480g
  --shm-size LIMIT            default: 32g
  --upload-model-outputs      include large output and cache directories
EOF
}

model_name=""
model_commit=""
artifact_uri=""
activitysim_commit=""
sharrow_commit=""
work_dir="/work"
processes=16
memory="480g"
shm_size="32g"
upload_model_outputs=false
sharrow_enabled=true
use_explicit_error_terms=false
smoke_test=false

while (($#)); do
  case "$1" in
    --model) model_name=${2:?}; shift 2 ;;
    --model-commit) model_commit=${2:?}; shift 2 ;;
    --artifact-uri) artifact_uri=${2:?}; shift 2 ;;
    --activitysim-commit) activitysim_commit=${2:?}; shift 2 ;;
    --sharrow-commit) sharrow_commit=${2:?}; shift 2 ;;
    --sharrow) sharrow_enabled=true; shift ;;
    --no-sharrow) sharrow_enabled=false; shift ;;
    --eet) use_explicit_error_terms=true; shift ;;
    --no-eet) use_explicit_error_terms=false; shift ;;
    --smoke-test) smoke_test=true; shift ;;
    --work-dir) work_dir=${2:?}; shift 2 ;;
    --processes) processes=${2:?}; shift 2 ;;
    --memory) memory=${2:?}; shift 2 ;;
    --shm-size) shm_size=${2:?}; shift 2 ;;
    --upload-model-outputs) upload_model_outputs=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ $model_name == mtc-extended ]]; then
  model_repository=https://github.com/ActivitySim/activitysim-prototype-mtc.git
  profile=mtc-extended
  data_name=data_full
elif [[ $model_name == sandag ]]; then
  model_repository=https://github.com/ActivitySim/sandag-abm3-example.git
  profile=sandag
  data_name=data-full
else
  echo "--model must be mtc-extended or sandag" >&2
  exit 2
fi

commit_pattern='^[0-9a-fA-F]{40}$'
for value in "$model_commit" "$activitysim_commit"; do
  if [[ ! $value =~ $commit_pattern ]]; then
    echo "All source revisions must be full 40-character commit SHAs" >&2
    exit 2
  fi
done
if [[ $sharrow_enabled == true && ! $sharrow_commit =~ $commit_pattern ]]; then
  echo "--sharrow requires --sharrow-commit with a full commit SHA" >&2
  exit 2
fi
if [[ -n $sharrow_commit && ! $sharrow_commit =~ $commit_pattern ]]; then
  echo "All source revisions must be full 40-character commit SHAs" >&2
  exit 2
fi
if [[ ! $artifact_uri =~ ^s3://[^/]+/.+ ]] || [[ ! $processes =~ ^[1-9][0-9]*$ ]]; then
  echo "Provide a non-root S3 artifact URI and a positive process count" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
abench_root=$(cd -- "$script_dir/../.." && pwd)
abench_commit=$(git -C "$abench_root" rev-parse HEAD)
results="$work_dir/results"
model="$work_dir/model"
metadata="$work_dir/release-metadata"
status_path="$work_dir/status.json"
output="$results/$model_name"
mkdir -p "$results" "$metadata" "$work_dir/data-cache"
exec > >(tee -a "$metadata/model-run.log") 2>&1

validate_rc=99
run_rc=99
population_rc=99
finalized=false

sync_artifacts() {
  local options=(--only-show-errors)
  if [[ $upload_model_outputs != true ]]; then
    options+=(
      --exclude '*/warmup/output/*'
      --exclude '*/measured/output/*'
      --exclude '*/cache/*'
    )
  fi
  aws s3 sync "$results/" "$artifact_uri/results/" "${options[@]}"
  aws s3 sync "$metadata/" "$artifact_uri/metadata/" --only-show-errors
}

write_status() {
  local state=$1
  local process_rc=$2
  STATE="$state" PROCESS_RC="$process_rc" STATUS_PATH="$status_path" \
  MODEL_NAME="$model_name" MODEL_COMMIT="$model_commit" \
  ARTIFACT_URI="$artifact_uri" ACTIVITYSIM_COMMIT="$activitysim_commit" \
  ABENCH_COMMIT="$abench_commit" SHARROW_COMMIT="$sharrow_commit" \
  PROCESSES="$processes" MEMORY="$memory" SHM_SIZE="$shm_size" \
  SHARROW_ENABLED="$sharrow_enabled" USE_EXPLICIT_ERROR_TERMS="$use_explicit_error_terms" \
  SMOKE_TEST="$smoke_test" \
  VALIDATE_RC="$validate_rc" RUN_RC="$run_rc" POPULATION_RC="$population_rc" \
  python3 - <<'PY'
import json
import os
from datetime import datetime, timezone
from pathlib import Path

validate_rc = int(os.environ["VALIDATE_RC"])
run_rc = int(os.environ["RUN_RC"])
population_rc = int(os.environ["POPULATION_RC"])
state = os.environ["STATE"]
document = {
    "schema_version": 1,
    "state": state,
    "success": state == "complete" and not any(
        (validate_rc, run_rc, population_rc)
    ),
    "process_returncode": int(os.environ["PROCESS_RC"]),
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "artifact_uri": os.environ["ARTIFACT_URI"],
    "model": os.environ["MODEL_NAME"],
    "configuration": {
        "processes": int(os.environ["PROCESSES"]),
        "memory": os.environ["MEMORY"],
        "shm_size": os.environ["SHM_SIZE"],
        "sharrow": os.environ["SHARROW_ENABLED"] == "true",
        "use_explicit_error_terms": os.environ["USE_EXPLICIT_ERROR_TERMS"] == "true",
        "smoke_test": os.environ["SMOKE_TEST"] == "true",
    },
    "commits": {
        "abench": os.environ["ABENCH_COMMIT"],
        "activitysim": os.environ["ACTIVITYSIM_COMMIT"],
        "sharrow": os.environ["SHARROW_COMMIT"] or None,
        "model": os.environ["MODEL_COMMIT"],
    },
    "validate_returncode": validate_rc,
    "run_returncode": run_rc,
    "population_check_returncode": population_rc,
}
Path(os.environ["STATUS_PATH"]).write_text(json.dumps(document, indent=2) + "\n")
PY
}

finish() {
  local rc=$?
  set +e
  if [[ $finalized != true ]]; then
    write_status failed "$rc"
    sync_artifacts
    aws s3 cp "$status_path" "$artifact_uri/status.json" --only-show-errors
  fi
}
trap finish EXIT

echo "Cloning $model_name at $model_commit"
git init -q "$model"
git -C "$model" remote add origin "$model_repository"
git -C "$model" fetch -q --depth 1 origin "$model_commit"
git -C "$model" -c advice.detachedHead=false checkout -q --detach FETCH_HEAD
[[ $(git -C "$model" rev-parse HEAD) == "${model_commit,,}" ]]

abench="$work_dir/abench-venv/bin/abench"
if [[ ! -x $abench ]]; then
  python3 -m venv "$work_dir/abench-venv"
  "$work_dir/abench-venv/bin/pip" install --quiet --upgrade pip
  "$work_dir/abench-venv/bin/pip" install --quiet "$abench_root"
fi
"$work_dir/abench-venv/bin/pip" freeze > "$metadata/host-pip-freeze.txt"
docker version > "$metadata/docker-version.txt"
uname -a > "$metadata/uname.txt"

check_source() {
  local name=$1
  local repository=$2
  local commit=$3
  local target="$work_dir/source-checkouts/$name"
  git init -q "$target"
  git -C "$target" remote add origin "$repository"
  git -C "$target" fetch -q --depth 1 origin "$commit"
  [[ $(git -C "$target" rev-parse FETCH_HEAD) == "${commit,,}" ]]
}

if [[ $smoke_test == true ]]; then
  echo "Running AWS smoke checks for $model_name"
  mkdir -p "$work_dir/source-checkouts"
  check_source activitysim \
    https://github.com/ActivitySim/activitysim.git "$activitysim_commit"
  if [[ $sharrow_enabled == true ]]; then
    check_source sharrow \
      https://github.com/ActivitySim/sharrow.git "$sharrow_commit"
  fi
  "$abench" --help > "$metadata/abench-help.txt"
  docker run --rm --platform linux/amd64 python:3.11-slim-bookworm \
    python --version > >(tee "$metadata/docker-smoke.log") 2>&1
  validate_rc=0
  run_rc=0
  population_rc=0
  write_status complete 0
  sync_artifacts
  aws s3 cp "$status_path" "$artifact_uri/status.json" --only-show-errors
  finalized=true
  trap - EXIT
  echo "AWS smoke checks passed for $model_name"
  exit 0
fi

echo "Installing the pinned data-download environment"
python3 -m venv "$work_dir/data-venv"
"$work_dir/data-venv/bin/pip" install --quiet --upgrade pip
download_packages=(
  "activitysim @ git+https://github.com/ActivitySim/activitysim.git@$activitysim_commit"
  'wring>=0.0.6'
)
if [[ $sharrow_enabled == true ]]; then
  download_packages+=(
    "sharrow @ git+https://github.com/ActivitySim/sharrow.git@$sharrow_commit"
  )
fi
"$work_dir/data-venv/bin/pip" install --quiet "${download_packages[@]}"
"$work_dir/data-venv/bin/pip" freeze > "$metadata/data-download-pip-freeze.txt"

echo "Downloading and verifying full-scale data for $model_name"
"$work_dir/data-venv/bin/python" "$script_dir/prepare_data.py" \
  "$model_name" "$model" --cache "$work_dir/data-cache"

common=(
  --model-dir "$model"
  --profile "$profile"
  --data-dir "$model/$data_name"
  --source "activitysim=ActivitySim/activitysim@$activitysim_commit"
  --multiprocess
  --processes "$processes"
  --households 0
  --memory "$memory"
  --shm-size "$shm_size"
  --platform linux/amd64
)
if [[ $sharrow_enabled == true ]]; then
  common+=(--source "sharrow=ActivitySim/sharrow@$sharrow_commit" --sharrow)
else
  common+=(--no-sharrow)
fi
if [[ $use_explicit_error_terms == true ]]; then
  common+=(--eet)
else
  common+=(--no-eet)
fi

echo "Preflighting $model_name"
set +e
"$abench" validate "${common[@]}" > >(tee "$metadata/validate.log") 2>&1
validate_rc=$?
set -e
sync_artifacts

if ((validate_rc == 0)); then
  echo "Running $model_name"
  set +e
  "$abench" run "${common[@]}" \
    --label "$model_name full population" --output-dir "$output" \
    > >(tee "$metadata/run.log") 2>&1
  run_rc=$?
  set -e
  if "$script_dir/check_full_population.py" "$output"; then
    population_rc=0
  else
    population_rc=$?
  fi
else
  echo "Skipping $model_name after failed preflight"
fi

overall_rc=0
for rc in "$validate_rc" "$run_rc" "$population_rc"; do
  if ((rc != 0)); then
    overall_rc=1
  fi
done

write_status complete "$overall_rc"
sync_artifacts
aws s3 cp "$status_path" "$artifact_uri/status.json" --only-show-errors
finalized=true
trap - EXIT
exit "$overall_rc"

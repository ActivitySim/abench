#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Provision independent EC2 instances for the full-scale ActivitySim release run.

Required:
  --bucket NAME
  --vpc-id ID
  --subnet-id ID
  --activitysim-commit SHA
  --sharrow-commit SHA

Optional:
  --mtc-extended-commit SHA    default: latest activitysim-prototype-mtc/extended
  --sandag-commit SHA          default: latest sandag-abm3-example/main
  --abench-commit SHA          default: current checkout
  --region REGION              default: AWS CLI configuration
  --prefix PREFIX              default: activitysim-release/<UTC timestamp>
  --stack-name NAME            base name; -mtc and -sandag are appended
  --mtc-instance-type TYPE     default: r7i.16xlarge
  --sandag-instance-type TYPE  default: r7i.16xlarge
  --mtc-processes N            default: 16
  --sandag-processes N         default: 16
  --memory LIMIT               default: 480g on each instance
  --shm-size LIMIT             default: 32g on each instance
  --volume-size GIB            default: 2048 on each instance
  --upload-model-outputs
  --no-wait                    return after both instances are launched
  --keep-stacks                do not delete stacks after a waited run
EOF
}

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
timestamp=$(date -u +%Y%m%d-%H%M%S)
bucket=""
vpc_id=""
subnet_id=""
activitysim_commit=""
sharrow_commit=""
mtc_extended_commit=""
sandag_commit=""
abench_commit=$(git -C "$repo_root" rev-parse HEAD)
region=""
prefix="activitysim-release/$timestamp"
stack_base="abench-release-$timestamp"
mtc_instance_type="r7i.16xlarge"
sandag_instance_type="r7i.16xlarge"
mtc_processes=16
sandag_processes=16
memory="480g"
shm_size="32g"
volume_size=2048
upload_model_outputs=false
wait_for_run=true
keep_stacks=false
abench_commit_explicit=false

while (($#)); do
  case "$1" in
    --bucket) bucket=${2:?}; shift 2 ;;
    --vpc-id) vpc_id=${2:?}; shift 2 ;;
    --subnet-id) subnet_id=${2:?}; shift 2 ;;
    --activitysim-commit) activitysim_commit=${2:?}; shift 2 ;;
    --sharrow-commit) sharrow_commit=${2:?}; shift 2 ;;
    --mtc-extended-commit) mtc_extended_commit=${2:?}; shift 2 ;;
    --sandag-commit) sandag_commit=${2:?}; shift 2 ;;
    --abench-commit) abench_commit=${2:?}; abench_commit_explicit=true; shift 2 ;;
    --region) region=${2:?}; shift 2 ;;
    --prefix) prefix=${2:?}; shift 2 ;;
    --stack-name) stack_base=${2:?}; shift 2 ;;
    --mtc-instance-type) mtc_instance_type=${2:?}; shift 2 ;;
    --sandag-instance-type) sandag_instance_type=${2:?}; shift 2 ;;
    --mtc-processes) mtc_processes=${2:?}; shift 2 ;;
    --sandag-processes) sandag_processes=${2:?}; shift 2 ;;
    --memory) memory=${2:?}; shift 2 ;;
    --shm-size) shm_size=${2:?}; shift 2 ;;
    --volume-size) volume_size=${2:?}; shift 2 ;;
    --upload-model-outputs) upload_model_outputs=true; shift ;;
    --no-wait) wait_for_run=false; shift ;;
    --keep-stacks) keep_stacks=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

commit_pattern='^[0-9a-fA-F]{40}$'
for value in "$abench_commit" "$activitysim_commit" "$sharrow_commit"; do
  if [[ ! $value =~ $commit_pattern ]]; then
    echo "Explicit revisions must be full 40-character commit SHAs" >&2
    exit 2
  fi
done
for value in "$mtc_extended_commit" "$sandag_commit"; do
  if [[ -n $value && ! $value =~ $commit_pattern ]]; then
    echo "Explicit revisions must be full 40-character commit SHAs" >&2
    exit 2
  fi
done
if [[ -z $bucket || -z $vpc_id || -z $subnet_id ]]; then
  echo "--bucket, --vpc-id, and --subnet-id are required" >&2
  usage >&2
  exit 2
fi
if [[ $abench_commit_explicit != true ]] &&
  [[ -n $(git -C "$repo_root" status --porcelain -- \
    release/aws src/abench/profiles/mtc-extended.yaml \
    src/abench/profiles.py src/abench/experiments.py) ]]; then
  echo "The AWS runner or mtc-extended profile has uncommitted changes." >&2
  echo "Commit and push them, or pass a known pushed --abench-commit SHA." >&2
  exit 2
fi
for value in "$mtc_processes" "$sandag_processes" "$volume_size"; do
  if [[ ! $value =~ ^[1-9][0-9]*$ ]]; then
    echo "Process counts and volume size must be positive integers" >&2
    exit 2
  fi
done
prefix=${prefix#/}
prefix=${prefix%/}
if [[ -z $prefix ]]; then
  echo "Artifact prefix must not be empty" >&2
  exit 2
fi

resolve_head() {
  local repository=$1
  local branch=$2
  local revision
  echo "Resolving latest $repository/$branch" >&2
  revision=$(git ls-remote --exit-code \
    "https://github.com/ActivitySim/$repository.git" "refs/heads/$branch" |
    awk 'NR == 1 {print $1}')
  if [[ ! $revision =~ $commit_pattern ]]; then
    echo "Could not resolve $repository branch $branch" >&2
    return 1
  fi
  echo "$revision"
}

if [[ -z $mtc_extended_commit ]]; then
  mtc_extended_commit=$(resolve_head activitysim-prototype-mtc extended)
fi
if [[ -z $sandag_commit ]]; then
  sandag_commit=$(resolve_head sandag-abm3-example main)
fi
echo "MTC Prototype Extended commit: $mtc_extended_commit"
echo "SANDAG commit: $sandag_commit"

aws_options=()
if [[ -n $region ]]; then
  aws_options+=(--region "$region")
fi

aws "${aws_options[@]}" s3api head-bucket --bucket "$bucket"
aws "${aws_options[@]}" cloudformation validate-template \
  --template-body "file://$script_dir/stack.yaml" >/dev/null

models=(mtc-extended sandag)
declare -A commits=(
  [mtc-extended]="$mtc_extended_commit"
  [sandag]="$sandag_commit"
)
declare -A instance_types=(
  [mtc-extended]="$mtc_instance_type"
  [sandag]="$sandag_instance_type"
)
declare -A process_counts=(
  [mtc-extended]="$mtc_processes"
  [sandag]="$sandag_processes"
)
declare -A stack_names=(
  [mtc-extended]="$stack_base-mtc"
  [sandag]="$stack_base-sandag"
)
declare -A instance_ids=()
declare -A deployed=()

deploy_model() {
  local model=$1
  local stack=${stack_names[$model]}
  local model_prefix="$prefix/$model"
  echo "Creating $stack for $model"
  aws "${aws_options[@]}" cloudformation deploy \
    --stack-name "$stack" \
    --template-file "$script_dir/stack.yaml" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
      VpcId="$vpc_id" \
      SubnetId="$subnet_id" \
      ArtifactBucket="$bucket" \
      ArtifactPrefix="$model_prefix" \
      AbenchCommit="$abench_commit" \
      ActivitySimCommit="$activitysim_commit" \
      SharrowCommit="$sharrow_commit" \
      Model="$model" \
      ModelCommit="${commits[$model]}" \
      InstanceType="${instance_types[$model]}" \
      Processes="${process_counts[$model]}" \
      Memory="$memory" \
      ShmSize="$shm_size" \
      RootVolumeSize="$volume_size" \
      UploadModelOutputs="$upload_model_outputs" \
    --no-fail-on-empty-changeset || return

  instance_ids[$model]=$(aws "${aws_options[@]}" cloudformation describe-stacks \
    --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" \
    --output text) || return
  deployed[$model]=true
  echo "$model instance: ${instance_ids[$model]}"
  echo "$model artifacts: s3://$bucket/$model_prefix"
  echo "Session: aws ssm start-session --target ${instance_ids[$model]}${region:+ --region $region}"
}

deployment_failed=false
for model in "${models[@]}"; do
  if ! deploy_model "$model"; then
    echo "Failed to deploy ${stack_names[$model]}" >&2
    deployment_failed=true
  fi
done

if [[ $wait_for_run != true ]]; then
  echo "Detached. Delete both stacks after their status.json objects appear."
  [[ $deployment_failed == false ]]
  exit
fi

echo "Waiting for both model statuses (up to 48 hours)"
status_dir=$(mktemp -d)
trap 'rm -rf "$status_dir"' EXIT
declare -A finished=()
declare -A missing=()
declare -A terminated_polls=()

for ((attempt = 0; attempt < 5760; attempt++)); do
  pending=false
  for model in "${models[@]}"; do
    [[ ${deployed[$model]:-false} == true ]] || continue
    [[ ${finished[$model]:-false} == true ]] && continue
    pending=true
    model_prefix="$prefix/$model"
    if aws "${aws_options[@]}" s3api head-object \
      --bucket "$bucket" --key "$model_prefix/status.json" >/dev/null 2>&1; then
      aws "${aws_options[@]}" s3 cp \
        "s3://$bucket/$model_prefix/status.json" "$status_dir/$model.json" \
        --only-show-errors
      finished[$model]=true
      echo "$model finished"
      continue
    fi
    state=$(aws "${aws_options[@]}" ec2 describe-instances \
      --instance-ids "${instance_ids[$model]}" \
      --query 'Reservations[0].Instances[0].State.Name' \
      --output text 2>/dev/null || true)
    if [[ $state == terminated ]]; then
      terminated_polls[$model]=$(( ${terminated_polls[$model]:-0} + 1 ))
      if ((terminated_polls[$model] >= 4)); then
        echo "$model terminated without a final status object" >&2
        missing[$model]=true
        finished[$model]=true
      fi
    fi
  done
  [[ $pending == false ]] && break
  sleep 30
done

result=0
[[ $deployment_failed == false ]] || result=1
for model in "${models[@]}"; do
  if [[ -f $status_dir/$model.json ]]; then
    echo "--- $model status ---"
    cat "$status_dir/$model.json"
    if ! python3 -c \
      'import json,sys; raise SystemExit(not json.load(open(sys.argv[1]))["success"])' \
      "$status_dir/$model.json"; then
      result=1
    fi
  else
    result=1
    missing[$model]=true
  fi
done

for model in "${models[@]}"; do
  [[ ${deployed[$model]:-false} == true ]] || continue
  stack=${stack_names[$model]}
  if [[ $keep_stacks == true || ${missing[$model]:-false} == true ]]; then
    echo "Stack retained: $stack"
  else
    echo "Waiting for $model instance to finish its final log upload"
    if ! aws "${aws_options[@]}" ec2 wait instance-terminated \
      --instance-ids "${instance_ids[$model]}"; then
      echo "Instance did not terminate cleanly; stack retained: $stack" >&2
      result=1
      continue
    fi
    echo "Deleting $stack"
    aws "${aws_options[@]}" cloudformation delete-stack --stack-name "$stack"
    aws "${aws_options[@]}" cloudformation wait stack-delete-complete \
      --stack-name "$stack"
  fi
done
exit "$result"

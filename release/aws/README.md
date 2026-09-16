# Manual full-scale release run on AWS

These scripts run MTC Prototype Extended and SANDAG at full population on two
independent EC2 instances. They are intentionally independent of GitHub Actions.
The local launcher creates one CloudFormation stack per model; each instance
downloads only its own checksummed public data, runs its benchmark, writes
durable artifacts to an existing S3 bucket, and terminates.

The stacks run concurrently and do not share compute, storage, or failure state.
A failed MTC Extended run does not prevent SANDAG from completing. Sharrow is a
launch-time choice. When enabled, it performs a complete warmup followed by the
measured run, so each model is run twice on its own host.

## Prerequisites

- AWS CLI v2 configured locally with permission to manage CloudFormation, EC2,
  IAM instance profiles, and the chosen S3 artifact prefix.
- An existing S3 bucket, preferably in the instance's region. The stack never
  owns or deletes the bucket.
- A VPC and subnet with outbound internet access. No inbound rule is created.
  A public subnet must assign a public IPv4 address; a private subnet needs NAT.
- EC2 quota and capacity for two chosen instances. Each default `r7i.16xlarge`
  provides 512 GiB RAM on x86-64 and requires 64 Standard-instance vCPUs, so
  launching both defaults requires a quota of at least 128 vCPUs.
- This abench commit pushed to `https://github.com/ActivitySim/abench.git` so the
  instance can check it out by its full SHA.

Use On-Demand capacity for a release gate. The runs are long and are not
resumable enough to make Spot interruptions a good default.

## Launch

Pass full 40-character commit SHAs for ActivitySim and Sharrow:

```bash
./release/aws/launch.sh \
  --bucket my-release-artifacts \
  --vpc-id vpc-0123456789abcdef0 \
  --subnet-id subnet-0123456789abcdef0 \
  --activitysim-commit <ACTIVITYSIM_SHA> \
  --sharrow-commit <SHARROW_SHA>
```

Sharrow is enabled by default and requires `--sharrow-commit`. To run the legacy
implementation instead, disable it and omit that commit:

```bash
./release/aws/launch.sh \
  --bucket my-release-artifacts \
  --vpc-id vpc-0123456789abcdef0 \
  --subnet-id subnet-0123456789abcdef0 \
  --activitysim-commit <ACTIVITYSIM_SHA> \
  --no-sharrow
```

By default, the launcher resolves the latest
`ActivitySim/activitysim-prototype-mtc` `extended` branch and the latest
`ActivitySim/sandag-abm3-example` `main` branch to immutable commit SHAs before
creating either stack. MTC cannot default to `main`: that branch does not contain
the required `ext-configs` and `ext-configs_mp` directories. To reproduce an
earlier run, override either revision with `--mtc-extended-commit <SHA>` or
`--sandag-commit <SHA>`.

The current abench checkout's commit is used by default. The launcher refuses to
use that default when the AWS runner or profile changes are uncommitted, because
an EC2 host can only clone pushed commits. Override it with a known pushed
`--abench-commit` SHA when appropriate.

The launcher waits for both final S3 status objects, prints them, then deletes
both temporary stacks. Use `--no-wait` to detach; each instance still terminates
itself after its run. In detached mode, delete both stacks after they finish to
remove their IAM roles and security groups.

Useful overrides include:

```bash
./release/aws/launch.sh ... \
  --region ap-southeast-2 \
  --mtc-processes 16 \
  --sandag-processes 16 \
  --mtc-instance-type r7i.16xlarge \
  --sandag-instance-type r7i.16xlarge \
  --volume-size 2048 \
  --prefix activitysim-release/1.6.0-rc1 \
  --upload-model-outputs
```

Each host defaults to 16 worker processes, a `480g` container limit, `32g` shared
memory, and its own 2 TiB encrypted gp3 root volume. This leaves memory for the
host and Docker. Each volume is deleted with its instance.

## AWS smoke test

Before paying for full-scale instances, run the same two-stack bootstrap in smoke
mode:

```bash
./release/aws/launch.sh \
  --bucket my-release-artifacts \
  --vpc-id vpc-0123456789abcdef0 \
  --subnet-id subnet-0123456789abcdef0 \
  --activitysim-commit <ACTIVITYSIM_SHA> \
  --no-sharrow \
  --smoke-test
```

Smoke mode defaults each stack to a `t3.small`, one process, and a 32 GiB gp3
volume. It verifies cloud-init package installation, the abench and model
checkouts, the pinned ActivitySim checkout, Docker image execution, instance-role
access to the artifact prefix, status upload, shutdown, and stack cleanup. It
does not install ActivitySim, download either full dataset, or run a model. With
`--sharrow --sharrow-commit <SHA>`, it also verifies that the pinned Sharrow
revision can be fetched.

Smoke results use `activitysim-smoke/<timestamp>/` by default and have the same
separate `mtc-extended/` and `sandag/` status layout as a release. Explicit
instance, process, volume, prefix, and stack-name options override smoke defaults.
Successful smoke statuses have `configuration.smoke_test: true`.

The launcher prints both instance IDs. While either is running, connect without
SSH:

```bash
aws ssm start-session --target <INSTANCE_ID> --region <REGION>
```

The bootstrap log on each host is `/var/log/abench-release.log`, and its live
model files are under `/work/results/<model>/measured/output`.

## Artifacts and acceptance checks

The chosen prefix gets separate `mtc-extended/` and `sandag/` directories. Each
contains its own `status.json`, `abench-release.log`, environment metadata, and
`results/` with reports, source provenance, resolved packages, console logs,
timing records, memory samples, and summaries. By default, large warmup/measured
model-output directories and caches are excluded. Pass `--upload-model-outputs`
to retain them too; apply an S3 lifecycle rule if they are only needed
temporarily.

A successful status requires, for both models:

- abench validation and execution exit successfully;
- the measured container is not OOM-killed and the abench report is valid; and
- measured input and output household row counts are equal.

That last check is explicit because `--households 0` selects the full population,
while abench's ordinary positive-sample check has no requested numeric row count
to compare in that mode.

MTC Extended uses the extended model configuration with the checksummed `data_full`
`v1.3.4` release asset documented by the MTC repository. SANDAG's own
`scripts/fulldata.py` downloads and validates the multipart `v0.2.0` dataset. If
either project publishes a new canonical dataset, update `prepare_data.py` or
the model repository pin as appropriate before the release run.

## Failure recovery

Each instance uploads a failure `status.json` and diagnostics when its runner can
do so, then shuts down. A systemd watchdog also terminates it after 18 hours.
Failures before the AWS CLI is installed may not reach S3; the launcher detects a
terminated instance without a status object and reports that condition. A stack
missing status is retained for diagnosis. Use its EC2 system log or
`/var/log/cloud-init-output.log` for early bootstrap failures.

# aws-admin-cli

A Typer + Boto3 command-line tool for administering AWS resources — safe by default against LocalStack, opt-in against real AWS via profiles.

![status](https://img.shields.io/badge/status-v1.0.0-success)
![tests](https://img.shields.io/badge/tests-1017%20passed-success)
![python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![aws-multi-region](https://img.shields.io/badge/AWS-Multi--Region-orange)

## Architecture

```text
src/aws_admin_cli/
  core/                    config, CLI context, exceptions, logging
  domain/
    models/                entities & value objects (common ledger record, IAM, S3, policy, VPC, findings)
    ports/                 abstract interfaces (Repository, IamGateway, S3Gateway, VpcGateway, ...)
    policies/              pure rule engines (sg_audit_rules.py) -- no I/O, no boto3
  application/
    dto/                   data transfer objects
    services/              cross-use-case logic (NetworkResolver) -- not itself a use case
    use_cases/{iam,s3,vpc,ec2,lambda,cloudwatch,audit}/   one file per use case
  infrastructure/
    aws/gateways/          boto3-backed adapters (Boto3IamGateway, Boto3S3Gateway, Boto3VpcGateway, ...)
    persistence/           local JSON resource ledger (atomic, per-profile)
  presentation/
    wiring.py              composition root: build use cases & stack engine
    cli/                   Typer commands (iam_app.py, s3_app.py, vpc_app.py, ec2_app.py, ...)
    cli/formatters/        Rich-based output
    tui/                   interactive mode: prompter.py (Prompter port), navigation.py, flows/
tests/
  unit/ integration/ e2e/ fakes/   fakes/ holds hand-written test doubles
  unit/architecture/       structural tests (e.g. the vpc-is-read-only guarantee)
docs/
localstack/init/          01-bootstrap.sh seeds a simulated corp network for `vpc`/e2e tests
```

See [docs/architecture.md](docs/architecture.md) for the full layer diagram and
[docs/aws-profiles.md](docs/aws-profiles.md) for how to point the CLI at LocalStack or
real AWS.

## Prerequisites

- Python 3.11 or 3.12
- [Poetry](https://python-poetry.org/) 2.x
- Docker (for LocalStack)

## Quickstart

```bash
poetry install
make up                        # start LocalStack
poetry run aws-admin-cli --version
poetry run aws-admin-cli --help
```

Copy `.env.example` to `.env` and see `docs/aws-profiles.md` for the AWS CLI profile
blocks to paste into `~/.aws/config` and `~/.aws/credentials` (nothing is modified for
you automatically).

## Editor setup (VS Code)

If VS Code's **Play ▶** button runs `/usr/local/bin/python3` instead of the Poetry
virtualenv — the symptom is `ModuleNotFoundError: No module named 'pydantic'` — see
**[DEVELOPMENT.md](DEVELOPMENT.md)**. It covers the two independent causes (Code
Runner's default executor, and `python.defaultInterpreterPath` being only a
*default*), the one manual step no config file can do for you, and the ready-made
tasks and debug configurations.

`.vscode/` is gitignored, so those files are local to each machine; DEVELOPMENT.md is
the recipe to recreate them.

## Usage

Global options (`--profile`, `--region`, `--endpoint-url`, `--output`, `--verbose`,
`--quiet`) go before the subcommand; `--output` may also be repeated after `config` or
`doctor` specifically:

```bash
# Show the effective configuration — no AWS call.
poetry run aws-admin-cli config

# Same, as JSON (safe to pipe: logs never touch STDOUT).
poetry run aws-admin-cli config --output json

# Full end-to-end check: credentials, network, and sts:GetCallerIdentity.
poetry run aws-admin-cli doctor

# Target a specific profile/region/endpoint explicitly.
poetry run aws-admin-cli --profile localstack --endpoint-url http://localhost:4566 doctor

# Verbose (DEBUG) logging, to STDERR.
poetry run aws-admin-cli --verbose doctor
```

## IAM

Users, customer-managed policies, and roles — grouped under `iam user` / `iam policy` /
`iam role`:

```bash
# Create a user (auto-tagged ManagedBy=aws-admin-cli, idempotent with --if-not-exists).
poetry run aws-admin-cli iam user create alice --tag Owner=ruben --if-not-exists

# List / get / delete.
poetry run aws-admin-cli iam user list --output json
poetry run aws-admin-cli iam user get alice
poetry run aws-admin-cli iam user delete alice --force --yes   # --force detaches policies first

# Create a policy from inline JSON or a file.
poetry run aws-admin-cli iam policy create read-only \
  --document-json '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:GetObject","Resource":"arn:aws:s3:::my-bucket/*"}]}'
poetry run aws-admin-cli iam policy create from-file --document-file ./policy.json

# Create a role for a service principal, or from a custom trust policy file.
poetry run aws-admin-cli iam role create ec2-role --service ec2.amazonaws.com
poetry run aws-admin-cli iam role create custom-role --trust-policy-file ./trust.json

# Attach / detach / list attachments (attach is idempotent).
poetry run aws-admin-cli iam role attach-policy ec2-role --policy-arn <ARN>
poetry run aws-admin-cli iam role policies ec2-role
```

`make demo-iam` runs a full create → attach → list → detach → delete sequence against
LocalStack.

**Wildcard guard rail:** `iam policy create` refuses to create a policy with an `Allow`
statement granting `"*"` action on `"*"` resource (unrestricted administrative access)
unless you pass `--allow-wildcard` — this is a deliberate speed bump against creating an
admin-equivalent policy by accident, not a hard security boundary. It fails with exit
code 64 and an explanation; add `--allow-wildcard` once you're sure that's what you want.

`delete` commands on a user/role with attached policies fail the same way (listing what's
attached) unless `--force` is passed, which detaches everything first. Every `delete`
also prompts for confirmation unless `--yes` is passed — in a non-interactive shell
(no TTY), it fails immediately asking for `--yes` rather than hanging.

## S3

Buckets under `s3 bucket`, plus top-level `s3 ls` / `s3 cp` / `s3 rm` / `s3 presign` for
objects (mirroring the `aws s3` CLI's shape rather than nesting everything under an
`object` sub-group):

```bash
# Create a bucket (Block Public Access is applied by default -- see below).
poetry run aws-admin-cli s3 bucket create my-bucket --region eu-west-1 --if-not-exists
poetry run aws-admin-cli s3 bucket create my-bucket --enable-versioning

# List / inspect / delete.
poetry run aws-admin-cli s3 bucket list --output json
poetry run aws-admin-cli s3 bucket info my-bucket
poetry run aws-admin-cli s3 bucket delete my-bucket --force --yes   # --force empties it first

# Versioning and tags.
poetry run aws-admin-cli s3 bucket versioning my-bucket --enable
poetry run aws-admin-cli s3 bucket tags my-bucket --tag Owner=ruben

# Bucket policy (guard rail below applies to `set`).
poetry run aws-admin-cli s3 bucket policy get my-bucket
poetry run aws-admin-cli s3 bucket policy set my-bucket --document-file ./policy.json
poetry run aws-admin-cli s3 bucket policy delete my-bucket --yes

# List objects (table shows human-readable size; --output json gives raw bytes).
poetry run aws-admin-cli s3 ls s3://my-bucket --prefix logs/ --recursive

# Copy: local -> s3 (file or whole directory), s3 -> local, or s3 -> s3.
poetry run aws-admin-cli s3 cp ./report.csv s3://my-bucket/reports/report.csv
poetry run aws-admin-cli s3 cp ./dist/ s3://my-bucket/dist/            # recursive, a directory
poetry run aws-admin-cli s3 cp s3://my-bucket/reports/report.csv ./report.csv
poetry run aws-admin-cli s3 cp s3://my-bucket/a.txt s3://my-bucket/b.txt

# Delete a single object, or everything under a prefix.
poetry run aws-admin-cli s3 rm s3://my-bucket/reports/report.csv --yes
poetry run aws-admin-cli s3 rm s3://my-bucket/reports/ --recursive --yes

# Presigned URL (hard-capped at 604800s / 7 days; warns above 1 day).
poetry run aws-admin-cli s3 presign s3://my-bucket/reports/report.csv --expires-in 900
```

`make demo-s3` runs a full create → upload → list → download → presign → empty → delete
sequence against LocalStack.

**Block Public Access by default:** `s3 bucket create` applies S3 Block Public Access
(all four flags) unless `--allow-public` is passed, which logs a WARNING to STDERR
instead. Similarly, `s3 bucket policy set` refuses an `Allow` statement with
`Principal: "*"` (or `{"AWS": "*"}`) unless `--allow-public` is passed — both are
deliberate speed bumps against accidentally making a bucket public, not a hard security
boundary.

Uploads/downloads use a Rich progress bar written to STDERR, auto-disabled when
`--output json` is set or STDERR isn't a TTY — `s3 cp ... --output json` always produces
clean, pipeable JSON on STDOUT.

## VPC

**Read-only.** The base network (VPCs, subnets, security groups, route tables,
internet/NAT gateways) belongs to Networking/SecOps, not to this tool — see
[docs/least-privilege.md](docs/least-privilege.md#separation-of-duties) for why. The
`vpc` module only ever calls `ec2:Describe*`; it has no `create`/`delete`/`modify` command
anywhere, on anything:

```bash
# VPCs.
poetry run aws-admin-cli vpc list
poetry run aws-admin-cli vpc show corp-main-vpc   # aggregate: VPC + its subnets + its SGs

# Subnets (table shows available IPs).
poetry run aws-admin-cli vpc subnet list --public
poetry run aws-admin-cli vpc subnet list --private --az us-east-1a
poetry run aws-admin-cli vpc subnet show corp-private-1a

# Security groups.
poetry run aws-admin-cli vpc sg list
poetry run aws-admin-cli vpc sg show corp-bastion-sg   # ingress/egress rules; open-to-world in red

# Security group audit: runs every rule in domain/policies/sg_audit_rules.py.
poetry run aws-admin-cli vpc sg audit
poetry run aws-admin-cli vpc sg audit --min-severity HIGH
poetry run aws-admin-cli vpc sg audit --min-severity CRITICAL --fail-on-findings   # CI gate

# Availability zones.
poetry run aws-admin-cli vpc az list

# Resolve human references to IDs.
poetry run aws-admin-cli vpc resolve --subnet corp-private-1a --sg corp-web-sg
```

`make seed` (re-)runs `localstack/init/01-bootstrap.sh` against a running LocalStack,
seeding a small network that stands in for what Networking would have already
delivered: one VPC, four subnets (two public, two private), and five security groups.

## EC2

**Consumes the network, never administers it.** `ec2 instance launch` resolves
`--subnet`/`--sg` through the same read-only `vpc` module above:

```bash
# AMIs: by known alias, or see what's available.
poetry run aws-admin-cli ec2 ami resolve amazon-linux-2023
poetry run aws-admin-cli ec2 ami list --owner amazon

# Key pairs: private material is written ONCE to PATH/NAME.pem at mode 0600.
poetry run aws-admin-cli ec2 keypair create demo-key --path ~/.ssh
poetry run aws-admin-cli ec2 keypair list
poetry run aws-admin-cli ec2 keypair delete demo-key --yes

# Launch: shows what every reference resolved to and asks for confirmation.
poetry run aws-admin-cli ec2 instance launch demo-web \
  --ami amazon-linux-2023 --type t3.micro \
  --subnet corp-private-1a --sg corp-web-sg \
  --iam-role demo-ec2-role --wait

# --dry-run validates the request without launching.
poetry run aws-admin-cli ec2 instance launch demo-web \
  --ami amazon-linux-2023 --type t3.micro \
  --subnet corp-private-1a --sg corp-web-sg --dry-run

# List / show / lifecycle.
poetry run aws-admin-cli ec2 instance list --managed-only
poetry run aws-admin-cli ec2 instance show demo-web            # by id or tag Name
poetry run aws-admin-cli ec2 instance stop demo-web --wait --yes
poetry run aws-admin-cli ec2 instance start demo-web --wait --yes
poetry run aws-admin-cli ec2 instance console demo-web         # debug a failed boot
poetry run aws-admin-cli ec2 instance terminate demo-web --wait --yes
```

**Guard rails** (each blocks the operation with exit code 64 until you explicitly opt in):

| Guard rail | Blocks | Bypass |
|---|---|---|
| Instance-type allowlist | Launching outside `t2`/`t3`/`t3a`/`m5`/`m6i` families | `--confirm-large` |
| Public IP confirmation | `--public-ip` without explicit confirmation | `--confirm-public` |
| Public IP into a private subnet | `--public-ip` targeting a subnet with no route to an IGW | Target a public subnet instead -- no bypass |
| User-data size | User-data over EC2's 16384-byte hard limit | Shrink it, or fetch from S3 -- no bypass |
| User-data secret scan | User-data containing AWS keys, PEM keys, passwords | Use `--iam-role` or Secrets Manager -- no bypass |
| `ManagedBy` tag on lifecycle | Acting on an instance this CLI didn't create | `--force` |
| Network creation | Never happens -- EC2 only *consumes* existing networks | Request from SecOps -- no bypass |

## Lambda & Serverless

A multi-region scanner that bypasses the single-region AWS console limitation, finding and inspecting functions globally.

```bash
# List all Lambda functions across ALL enabled AWS regions in parallel.
poetry run aws-admin-cli lambda list --all-regions

# Inspect runtime, handler, and environment variables for a specific function.
poetry run aws-admin-cli lambda info my-function-name
```

## CloudWatch Observability

Separated from core audit workflows, focused purely on metrics, logs, and alerting.

```bash
# Identify monitored vs unmonitored EC2 instances.
poetry run aws-admin-cli cloudwatch alarms ec2-status

# Attach standard recovery/billing alarms to instances.
poetry run aws-admin-cli cloudwatch alarms attach --instance-id i-1234567890abcdef0
```

## Security & Compliance Audit

A dedicated module for cross-resource exposure detection and hygiene checks.

```bash
# Analyze IAM privilege escalation risks and stale credentials.
poetry run aws-admin-cli audit iam-insights

# Detect S3 buckets missing lifecycle rules or public access blocks.
poetry run aws-admin-cli audit s3-governance

# Identify stale stopped EC2 instances racking up EBS costs.
poetry run aws-admin-cli audit ec2-stale-instances
```

## Stack

A declarative orchestration engine: a YAML manifest declares a set of IAM/S3/EC2
resources plus their dependencies, `stack apply` creates them in dependency order, and
a failure mid-apply triggers automatic saga-pattern rollback.

```bash
# Validate, then preview the plan -- neither touches AWS.
poetry run aws-admin-cli stack validate examples/webapp-stack.yaml
poetry run aws-admin-cli stack plan examples/webapp-stack.yaml

# Apply: shows the plan and asks for confirmation unless --yes.
poetry run aws-admin-cli stack apply examples/webapp-stack.yaml --yes

# Inspect state; --refresh checks every resource against AWS for drift.
poetry run aws-admin-cli stack list
poetry run aws-admin-cli stack show demo-webapp
poetry run aws-admin-cli stack status demo-webapp --refresh

# Destroy everything this stack created.
poetry run aws-admin-cli stack destroy demo-webapp --yes
```

### Exit codes

| Code | Meaning                              | Raised by                                   |
|------|---------------------------------------|----------------------------------------------|
| 0    | Success                                |                                                |
| 1    | Unclassified AWS error                 | `AwsError`, `UnknownAwsError`                 |
| 3    | `vpc sg audit --fail-on-findings` found a finding >= `--min-severity` | `typer.Exit(code=3)`, `vpc sg audit` only |
| 4    | AWS resource not found, or unknown stack name | `ResourceNotFoundError`, `StackNotFoundError` |
| 5    | AWS resource already exists            | `ResourceAlreadyExistsError`                  |
| 64   | Invalid usage / AWS validation error   | `ValidationError` (`EX_USAGE`)                |
| 69   | Service unreachable (AWS or LocalStack)| `ServiceUnavailableError` (`EX_UNAVAILABLE`)  |
| 70   | `stack apply` failed; see `.orphaned` for rollback details | `StackApplyError` |
| 74   | Local persistence failure              | `PersistenceError` (`EX_IOERR`)               |
| 75   | AWS throttling, or a `--wait` timeout   | `ThrottlingError`, `OperationTimeoutError`    |
| 77   | Access denied / no usable credentials  | `AccessDeniedError`, `MissingCredentialsError`|
| 78   | Configuration error (bad profile/region) | `ConfigurationError`, `ProfileNotFoundError`  |
| 130  | Cancelled by the user (Ctrl-C)          |                                                |

Set `AWS_ADMIN_CLI_DEBUG_TRACEBACK=1` to get a raw Python traceback.

## Modo interactivo

Every command above also works exactly the same, unscripted: run `aws-admin-cli`
with no subcommand (or pass `--interactive`/`-i` explicitly) to get an arrow-key-navigable
menu instead of having to remember flags.

```bash
aws-admin-cli                    # launches the TUI, if stdin+stdout are both a real TTY
aws-admin-cli --interactive      # forces it, even without a TTY
aws-admin-cli -i --profile prod  # global options still apply
```

**When it launches, and when it doesn't:** a bare invocation launches the TUI only when
both stdin and stdout are a real terminal. Piped or scripted invocations fall back to
printing help and exiting `2`. Set `AWS_ADMIN_CLI_NO_INTERACTIVE=1` to force that same
fallback even from a real terminal.

**What it looks like:** the main menu lists exactly 7 modules in order: Environment, IAM, EC2, Lambda, S3, CloudWatch, and Audit, plus "Configuración actual" (`config`) and "Diagnóstico" (`doctor`).

## Roadmap

| Phase | Scope                                                        | Status      |
|-------|---------------------------------------------------------------|-------------|
| 0     | Scaffolding: structure, tooling, quality, LocalStack, bare CLI | ✅ Complete |
| 1     | Core: config, context, exceptions, logging, client factory     | ✅ Complete |
| 2     | Domain models & ports, local JSON resource ledger              | ✅ Complete |
| 3     | IAM use cases & CLI commands                                  | ✅ Complete |
| 4     | S3 use cases & CLI commands                                   | ✅ Complete |
| 5     | VPC use cases & CLI commands (read-only) & SG audit engine       | ✅ Complete |
| 6     | EC2 use cases & CLI commands                                   | ✅ Complete |
| 7     | `stack`: declarative orchestration engine (saga-pattern)       | ✅ Complete |
| 7.1   | Interactive TUI (`--interactive`/`-i`, arrow-key menus)       | ✅ Complete |
| 8     | Lambda, CloudWatch & Audit module implementation               | ✅ Complete |
| 9     | Production hardening & v1.0.0 release                          | ✅ Complete |

## Testing

The project is backed by a massive suite of **1,017 unit and integration tests** (100% passing).

```bash
make test        # unit + integration tests (1,017 tests)
make test-e2e     # requires LocalStack running (make up)
make doctor       # run CLI diagnostic check
make cov          # coverage with HTML report in htmlcov/
make check        # lint + typecheck + test
```
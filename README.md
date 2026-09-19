# aws-admin-cli

A Typer + Boto3 command-line tool for administering AWS resources — safe by default
against LocalStack, opt-in against real AWS via profiles.

![status](https://img.shields.io/badge/status-scaffolding-lightgrey)
![python](https://img.shields.io/badge/python-3.12-blue)
![license](https://img.shields.io/badge/license-TBD-lightgrey)

## Architecture

```
src/aws_admin_cli/
  core/                    config, CLI context, exceptions, logging
  domain/
    models/                entities & value objects (common ledger record, IAM, S3, policy, VPC, findings)
    ports/                 abstract interfaces (Repository, IamGateway, S3Gateway, VpcGateway, ...)
    policies/              pure rule engines (sg_audit_rules.py) -- no I/O, no boto3
  application/
    dto/                   data transfer objects
    services/              cross-use-case logic (NetworkResolver) -- not itself a use case
    use_cases/{iam,s3,vpc,ec2}/   one file per use case
  infrastructure/
    aws/gateways/          boto3-backed adapters (Boto3IamGateway, Boto3S3Gateway, Boto3VpcGateway, ...)
    persistence/           local JSON resource ledger (atomic, per-profile)
  presentation/
    wiring.py              composition root: build_{iam,s3,vpc,ec2}_use_cases(ctx), build_stack_engine(ctx)
    cli/                   Typer commands (iam_app.py, s3_app.py, vpc_app.py, confirm.py, ...)
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

- Python 3.12
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

# Subnets (table shows available IPs — the number you'll need in Fase 5 to pick
# where to launch an instance).
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

# Resolve human references to IDs (diagnostic, ahead of Fase 5's EC2 --subnet/--sg flags).
poetry run aws-admin-cli vpc resolve --subnet corp-private-1a --sg corp-web-sg
```

`make seed` (re-)runs `localstack/init/01-bootstrap.sh` against a running LocalStack,
seeding a small network that stands in for what Networking would have already
delivered: one VPC, four subnets (two public, two private, with a real internet-gateway
route so the public/private classification is genuine, not guessed), and five security
groups -- three of them deliberately misconfigured so `vpc sg audit` has real findings to
report. `make demo-vpc` walks through the read-only commands above against that seeded
network.

## EC2

**Consumes the network, never administers it.** `ec2 instance launch` resolves
`--subnet`/`--sg` through the same read-only `vpc` module above -- if the security group
you asked for doesn't exist, the command fails and tells you to request it from
Networking/SecOps; it is never created on your behalf. See
[docs/least-privilege.md](docs/least-privilege.md#separation-of-duties) for why, and
[docs/architecture.md](docs/architecture.md#el-flujo-de-ec2-instance-launch) for the full
resolution flow:

```bash
# AMIs: by known alias (newest match wins), or see what's available.
poetry run aws-admin-cli ec2 ami resolve amazon-linux-2023
poetry run aws-admin-cli ec2 ami list --owner amazon

# Key pairs: private material is written ONCE to PATH/NAME.pem at mode 0600,
# never logged, never in --output json -- and never overwritten.
poetry run aws-admin-cli ec2 keypair create demo-key --path ~/.ssh
poetry run aws-admin-cli ec2 keypair list
poetry run aws-admin-cli ec2 keypair delete demo-key --yes

# Launch: shows what every reference resolved to (AMI, subnet+AZ, SGs, IAM
# profile) and asks for confirmation before creating anything, unless --yes.
poetry run aws-admin-cli ec2 instance launch demo-web \
  --ami amazon-linux-2023 --type t3.micro \
  --subnet corp-private-1a --sg corp-web-sg \
  --iam-role demo-ec2-role --wait

# --dry-run validates the request (permissions + parameters) without launching.
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

`make demo-ec2` runs a full role → launch → show → stop → start → terminate sequence
against LocalStack.

**Guard rails** (each blocks the operation with exit code 64 until you explicitly opt in):

| Guard rail | Blocks | Bypass |
|---|---|---|
| Instance-type allowlist | Launching outside `t2`/`t3`/`t3a`/`m5`/`m6i` families | `--confirm-large` |
| Public IP confirmation | `--public-ip` without explicit confirmation | `--confirm-public` |
| Public IP into a private subnet | `--public-ip` targeting a subnet with no route to an IGW (would never actually get one) | Target a public subnet instead -- there is no bypass |
| User-data size | User-data over EC2's 16384-byte hard limit | Shrink it, or fetch it from S3 at boot instead of inlining it -- no bypass |
| User-data secret scan | User-data that looks like it contains an AWS key, a PEM private key, `password=`, or `token=` (IMDS makes user-data readable by any process on the instance) | Use `--iam-role` or Secrets Manager instead -- no bypass |
| `ManagedBy` tag on stop/reboot/terminate | Acting on an instance this CLI didn't create/tag itself | `--force` |
| Security group / subnet / instance profile creation | Never happens -- EC2 only *consumes* the network and roles that already exist | Request the resource from Networking/SecOps/IAM owners -- no bypass, by design |

**Real cost warning:** unlike `vpc sg audit` or the read-only modules, `ec2 instance
launch` creates billable resources on real AWS -- compute (while `RUNNING`) and EBS
volumes (even while `STOPPED`, see `InstanceState.is_billable`'s docstring). This CLI
deliberately shows no per-hour price estimate (see `domain/models/ec2.py`'s module
docstring for why); check the AWS Pricing Calculator or Cost Explorer before launching
anything outside the default allowlisted families, and remember to `ec2 instance
terminate` what you no longer need.

**Exit code 3:** `vpc sg audit --fail-on-findings` exits `3` if at least one finding at or
above `--min-severity` was found (default `INFO`, so effectively "any finding" unless you
raise it) — `0` otherwise, always, with or without `--fail-on-findings`. This is what
makes it usable as a CI gate: `aws-admin-cli vpc sg audit --min-severity CRITICAL
--fail-on-findings` fails the pipeline exactly when a critical exposure exists, and only
then.

## Stack

A declarative orchestration engine: a YAML manifest declares a set of IAM/S3/EC2
resources plus their dependencies, `stack apply` creates them in dependency order, and
a failure mid-apply triggers automatic saga-pattern rollback -- compensating, in exact
reverse order, only what THIS apply actually created. See
[docs/stack-manifest.md](docs/stack-manifest.md) for the full `kind` reference and
[docs/architecture.md](docs/architecture.md#el-motor-de-stacks-patrón-saga) for how the
rollback guarantee works, and [examples/](examples/) for a full worked example plus the
interpolation/dependency syntax.

```bash
# Validate, then preview the plan (topological order + CREATE/NO-OP/REPLACE) --
# neither touches AWS.
poetry run aws-admin-cli stack validate examples/webapp-stack.yaml
poetry run aws-admin-cli stack plan examples/webapp-stack.yaml

# Apply: shows the plan and asks for confirmation unless --yes. --dry-run validates
# without creating anything.
poetry run aws-admin-cli stack apply examples/webapp-stack.yaml --yes
poetry run aws-admin-cli stack apply examples/webapp-stack.yaml --yes  # idempotent: no-op

# Inspect state; --refresh checks every resource against AWS for drift.
poetry run aws-admin-cli stack list
poetry run aws-admin-cli stack show demo-webapp
poetry run aws-admin-cli stack status demo-webapp --refresh

# Destroy everything this stack created (never anything it merely referenced/reused).
poetry run aws-admin-cli stack destroy demo-webapp --yes
```

`make demo-stack` runs a full apply → status → destroy sequence against LocalStack.

**Consumes the network, never administers it** -- exactly like `ec2 instance launch`:
no `kind` in the manifest catalog can declare a VPC, subnet, or security group (and
never will -- see [docs/least-privilege.md](docs/least-privilege.md#separation-of-duties)).
An `ec2:instance` resource's `subnet`/`security_groups` properties resolve by name
against the existing network, read-only; a manifest that names one that doesn't exist
fails with the same "ask Networking" message the EC2 module gives.

**Rollback, not transactions:** AWS has no cross-service transaction to wrap a multi-resource
apply in, so each successful step records how to undo itself, and a later failure
triggers those compensations in exact reverse order -- one at a time, each wrapped so a
cleanup failure never aborts the rest. Only resources THIS apply actually created
(`created_by_stack=true`) are ever compensated or destroyed; a resource a manifest
merely found already existing and reused is never touched, by any `stack` command,
ever.

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
| 70   | `stack apply` failed; see `.orphaned` for what (if anything) rollback didn't clean up | `StackApplyError` |
| 74   | Local persistence failure              | `PersistenceError` (`EX_IOERR`, Fase 2+)      |
| 75   | AWS throttling, or a `--wait` timeout   | `ThrottlingError`, `OperationTimeoutError` (`EX_TEMPFAIL`) |
| 77   | Access denied / no usable credentials  | `AccessDeniedError`, `MissingCredentialsError` (`EX_NOPERM`) |
| 78   | Configuration error (bad profile, region, endpoint_url) | `ConfigurationError`, `ProfileNotFoundError` (`EX_CONFIG`) |
| 130  | Cancelled by the user (Ctrl-C)          |                                                |

Set `AWS_ADMIN_CLI_DEBUG_TRACEBACK=1` to get a raw Python traceback instead of the
formatted error message — useful when developing, never needed for normal use.

## Modo interactivo

Every command above also works exactly the same, unscripted: run `aws-admin-cli`
with no subcommand (or pass `--interactive`/`-i` explicitly) to get an arrow-key-navigable
menu instead of having to remember flags.

```bash
aws-admin-cli                    # launches the TUI, if stdin+stdout are both a real TTY
aws-admin-cli --interactive      # forces it, even without a TTY (e.g. inside `script`)
aws-admin-cli -i --profile prod  # global options (--profile/--region/...) still apply
```

**When it launches, and when it doesn't:** a bare invocation launches the TUI only when
both stdin and stdout are a real terminal -- piped, redirected, or scripted invocations
(`aws-admin-cli | cat`, a CI job, `cli_runner.invoke(app, [])` in a test) fall back to
printing help and exiting `2`, exactly like before this feature existed. Set
`AWS_ADMIN_CLI_NO_INTERACTIVE=1` to force that same fallback even from a real terminal
(useful over certain SSH/tmux setups that report as a TTY but aren't one you want a
menu in) -- `--interactive`/`-i`, if also passed, always wins over the env var, since an
explicit flag is a more specific instruction than an environment default.

**What it looks like:** a header names the tool version, active profile/region, and
whether you're pointed at LocalStack or real AWS (real AWS gets a loud warning -- this
tool defaults to LocalStack-first development). The main menu lists IAM, S3, VPC, EC2,
and Stack, plus "Configuración actual" (the `config` command's output) and "Diagnóstico"
(`doctor`). Every submenu ends with "&larr; Volver" (back) and "Salir" (exit); Ctrl-C
backs out one level, or exits entirely from the main menu.

**The same rules apply, just interactively instead of via flags:**
- The VPC menu is exactly as read-only as `vpc` on the command line -- there's no
  create/modify/delete option anywhere in it, on purpose (see
  [docs/least-privilege.md](docs/least-privilege.md#separation-of-duties)).
- A destructive guard rail (deleting a non-empty bucket, a user/role with attached
  policies, terminating an instance this CLI didn't launch) behaves the same as its
  `--force`-flag CLI equivalent: the plain action is tried first, and only if the
  underlying use case rejects it do you get asked whether to retry with the escape
  hatch -- the TUI never pre-checks a guard rail's condition itself.
- A `stack apply` failure shows the same three-way failed/cleaned/orphaned breakdown
  the CLI's error output does, live, as each resource is created or rolled back.

**Disabling it entirely:** set `AWS_ADMIN_CLI_NO_INTERACTIVE=1` in your shell profile or
CI environment if you never want a bare invocation to launch the menu, regardless of TTY.

## Roadmap

| Phase | Scope                                                        | Status      |
|-------|---------------------------------------------------------------|-------------|
| 0     | Scaffolding: structure, tooling, quality, LocalStack, bare CLI | ✅ Complete |
| 1     | Core: config, context, exceptions, logging, client factory, `doctor` | ✅ Complete |
| 2     | Domain models & ports, local JSON resource ledger              | ✅ Complete |
| 3     | IAM use cases & CLI commands (users, policies, roles)           | ✅ Complete |
| 4     | S3 use cases & CLI commands (buckets, objects)                  | ✅ Complete |
| 5     | VPC use cases & CLI commands (read-only) & SG audit engine       | ✅ Complete |
| 6     | EC2 use cases & CLI commands                                   | ✅ Complete |
| 7     | `stack`: declarative orchestration engine (manifest, saga-pattern rollback) | ✅ Complete |
| 7.1   | Interactive TUI (`--interactive`/`-i`, arrow-key menus over the same use cases) | ✅ Complete |
| 8     | Integration & e2e test coverage against LocalStack             | ⬜ Planned  |
| 9     | Real-AWS hardening, docs, release                               | ⬜ Planned  |

## Testing

```bash
make test        # unit + integration, excludes e2e
make test-e2e     # requires LocalStack running (make up)
make doctor       # poetry run aws-admin-cli doctor
make cov          # coverage with HTML report in htmlcov/
make check        # lint + typecheck + test
```

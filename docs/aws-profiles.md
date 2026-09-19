# AWS profiles

`aws_admin_cli` never modifies your system's `~/.aws/config` or `~/.aws/credentials`.
Instead, this document tells you exactly what to paste into them yourself.

## LocalStack profile

Add this block to `~/.aws/config`:

```ini
[profile localstack]
region = us-east-1
output = json
```

Add this block to `~/.aws/credentials`:

```ini
[localstack]
aws_access_key_id = test
aws_secret_access_key = test
```

LocalStack accepts any non-empty static credentials — `test`/`test` is the community
convention and carries no special meaning.

## Where the endpoint comes from

Note that neither block above sets an `endpoint_url`. AWS CLI profile files have no
first-class place for a custom endpoint that `aws_admin_cli` should rely on generically
across services. Instead, the LocalStack endpoint (`http://localhost:4566`) is injected
at runtime, in Fase 1, via one of:

- the `AWS_ADMIN_CLI_ENDPOINT_URL` environment variable (see `.env.example`), or
- the `--endpoint-url` CLI flag, which takes precedence when passed explicitly.

This keeps the `localstack` profile identical in shape to a real AWS profile — only the
endpoint differs, and that's controlled by `aws_admin_cli` itself, not by AWS config
files. Real AWS profiles you already have configured continue to work untouched; select
them via `AWS_ADMIN_CLI_PROFILE` or `--profile` without setting an endpoint override.

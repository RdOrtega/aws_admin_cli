"""Integration tests for the `stack` CLI commands, via CliRunner + moto."""

import json
from pathlib import Path

import boto3
from aws_admin_cli.core.exceptions import AwsAdminCliError
from aws_admin_cli.main import app
from moto import mock_aws
from typer.testing import CliRunner

_BASE_ARGS = ["--profile", "testprofile"]

# See tests/integration/test_iam_cli.py's module docstring/comment: CliRunner bypasses
# main.run()'s sys.exit(exc.exit_code) translation, so result.exit_code is always 1 for
# any raised AwsAdminCliError -- the real per-error exit code lives on result.exception.

_VALID_MANIFEST = """\
apiVersion: v1
name: cli-test-stack
resources:
  - id: app-role
    kind: iam:role
    properties:
      role_name: cli-test-role
      service: ec2.amazonaws.com
"""

_NETWORK_MANIFEST = """\
apiVersion: v1
name: cli-test-network
resources:
  - id: extra-sg
    kind: security-group:sg
    properties:
      group_name: not-allowed
"""


@mock_aws
def test_validate_a_valid_manifest_succeeds(cli_runner: CliRunner, tmp_path: Path) -> None:
    manifest_path = tmp_path / "stack.yaml"
    manifest_path.write_text(_VALID_MANIFEST)

    result = cli_runner.invoke(app, [*_BASE_ARGS, "stack", "validate", str(manifest_path)])

    assert result.exit_code == 0, result.output


@mock_aws
def test_validate_a_network_resource_fails_with_separation_of_duties_message(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "network-stack.yaml"
    manifest_path.write_text(_NETWORK_MANIFEST)

    result = cli_runner.invoke(app, [*_BASE_ARGS, "stack", "validate", str(manifest_path)])

    assert isinstance(result.exception, AwsAdminCliError)
    assert result.exception.exit_code == 64
    message = str(result.exception) + str(result.exception.hint)
    assert "Networking/SecOps" in message or "Separation of Duties" in message


@mock_aws
def test_plan_json_output_is_parseable_and_shows_the_order(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "stack.yaml"
    manifest_path.write_text(_VALID_MANIFEST)

    result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--output", "json", "stack", "plan", str(manifest_path)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload[0]["LogicalId"] == "app-role"
    assert payload[0]["Action"] == "CREATE"
    assert payload[0]["Order"] == 0


@mock_aws
def test_apply_dry_run_creates_nothing(cli_runner: CliRunner, tmp_path: Path) -> None:
    manifest_path = tmp_path / "stack.yaml"
    manifest_path.write_text(_VALID_MANIFEST)

    result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--output", "json", "stack", "apply", str(manifest_path), "--dry-run"]
    )

    assert result.exit_code == 0, result.output

    iam_client = boto3.client("iam", region_name="us-east-1")
    roles = iam_client.list_roles()["Roles"]
    assert not any(role["RoleName"] == "cli-test-role" for role in roles)


@mock_aws
def test_apply_failure_with_no_rollback_lists_orphans_and_exits_70(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "broken.yaml"
    manifest_path.write_text(
        "apiVersion: v1\n"
        "name: cli-orphan-stack\n"
        "resources:\n"
        "  - id: app-bucket\n"
        "    kind: s3:bucket\n"
        "    properties:\n"
        "      bucket_name: cli-orphan-bucket\n"
        "  - id: attach-policy\n"
        "    kind: iam:policy-attachment\n"
        "    properties:\n"
        "      policy_arn: arn:aws:iam::123456789012:policy/does-not-exist\n"
        "      principal_type: role\n"
        "      principal_name: no-such-role\n"
        "    depends_on: [app-bucket]\n"
    )

    result = cli_runner.invoke(
        app,
        [
            *_BASE_ARGS,
            "stack",
            "apply",
            str(manifest_path),
            "--yes",
            "--no-rollback",
        ],
    )

    assert isinstance(result.exception, AwsAdminCliError)
    assert result.exception.exit_code == 70
    assert "huérfano" in result.output
    assert "app-bucket" in result.output


@mock_aws
def test_list_show_status_and_destroy_full_lifecycle(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "stack.yaml"
    manifest_path.write_text(_VALID_MANIFEST)
    apply_result = cli_runner.invoke(
        app, [*_BASE_ARGS, "stack", "apply", str(manifest_path), "--yes"]
    )
    assert apply_result.exit_code == 0, apply_result.output

    list_result = cli_runner.invoke(app, [*_BASE_ARGS, "--output", "json", "stack", "list"])
    assert list_result.exit_code == 0, list_result.output
    listed = json.loads(list_result.stdout)
    assert any(s["name"] == "cli-test-stack" for s in listed)

    show_result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--output", "json", "stack", "show", "cli-test-stack"]
    )
    assert show_result.exit_code == 0, show_result.output
    shown = json.loads(show_result.stdout)
    assert shown["name"] == "cli-test-stack"
    assert shown["status"] == "applied"

    status_result = cli_runner.invoke(
        app,
        [*_BASE_ARGS, "--output", "json", "stack", "status", "cli-test-stack", "--refresh"],
    )
    assert status_result.exit_code == 0, status_result.output
    refreshed = json.loads(status_result.stdout)
    assert all(r["status"] == "created" for r in refreshed["resources"])

    destroy_result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--output", "json", "stack", "destroy", "cli-test-stack", "--yes"]
    )
    assert destroy_result.exit_code == 0, destroy_result.output
    destroyed = json.loads(destroy_result.stdout)
    assert destroyed["status"] == "destroyed"

    iam_client = boto3.client("iam", region_name="us-east-1")
    roles = iam_client.list_roles()["Roles"]
    assert not any(role["RoleName"] == "cli-test-role" for role in roles)


@mock_aws
def test_show_and_status_unknown_stack_exit_4(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, [*_BASE_ARGS, "stack", "show", "no-existe-este-stack"])

    assert isinstance(result.exception, AwsAdminCliError)
    assert result.exception.exit_code == 4


@mock_aws
def test_destroy_without_yes_in_non_interactive_env_fails_cleanly(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    manifest_path = tmp_path / "stack.yaml"
    manifest_path.write_text(_VALID_MANIFEST)
    cli_runner.invoke(app, [*_BASE_ARGS, "stack", "apply", str(manifest_path), "--yes"])

    result = cli_runner.invoke(app, [*_BASE_ARGS, "stack", "destroy", "cli-test-stack"])

    assert isinstance(result.exception, AwsAdminCliError)
    assert result.exception.exit_code == 64
    assert "--yes" in str(result.exception.hint)

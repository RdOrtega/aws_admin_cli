"""Integration tests for the `iam` CLI commands, via CliRunner + moto."""

import json

from aws_admin_cli.core.exceptions import AwsAdminCliError
from aws_admin_cli.main import app
from moto import mock_aws
from typer.testing import CliRunner

_BASE_ARGS = ["--profile", "testprofile"]

# CliRunner.invoke(app, ...) calls the Typer app directly, bypassing main.run()'s
# sys.exit(exc.exit_code) translation entirely (Click's own test runner only maps
# ClickException/Abort to a specific exit code; any other exception -- including
# our AwsAdminCliError subclasses -- always surfaces as result.exit_code == 1,
# with the real exception object, exit_code and all, on result.exception). The
# real per-error exit code (64, 78, ...) is what run() produces, and is exercised
# by the manual/e2e verification instead; here we assert against the exception's
# own .exit_code, which is the value run() would have passed to sys.exit().


@mock_aws
def test_iam_user_create_json_output_contains_user_name(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--output", "json", "iam", "user", "create", "alice"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["UserName"] == "alice"


@mock_aws
def test_iam_user_delete_without_yes_in_non_interactive_env_fails_cleanly(
    cli_runner: CliRunner,
) -> None:
    cli_runner.invoke(app, [*_BASE_ARGS, "iam", "user", "create", "alice"])

    # CliRunner's stdin is never a TTY, so this must hit the "no --yes, no
    # terminal" ValidationError path rather than hanging waiting for input.
    result = cli_runner.invoke(app, [*_BASE_ARGS, "iam", "user", "delete", "alice"])

    assert isinstance(result.exception, AwsAdminCliError)
    assert result.exception.exit_code == 64
    assert "--yes" in str(result.exception.hint)


@mock_aws
def test_iam_policy_create_wildcard_without_allow_flag_fails_with_exit_64(
    cli_runner: CliRunner,
) -> None:
    wildcard_document = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }
    )

    result = cli_runner.invoke(
        app,
        [
            *_BASE_ARGS,
            "iam",
            "policy",
            "create",
            "admin-policy",
            "--document-json",
            wildcard_document,
        ],
    )

    assert isinstance(result.exception, AwsAdminCliError)
    assert result.exception.exit_code == 64
    assert "allow-wildcard" in str(result.exception.hint)


@mock_aws
def test_iam_user_list_json_stdout_is_clean_of_logs(cli_runner: CliRunner) -> None:
    cli_runner.invoke(app, [*_BASE_ARGS, "iam", "user", "create", "alice"])

    result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--verbose", "--output", "json", "iam", "user", "list"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert any(user["UserName"] == "alice" for user in payload)

"""Integration test for `doctor`, using moto to mock STS (no real AWS or LocalStack)."""

import json

from aws_admin_cli.main import app
from moto import mock_aws
from typer.testing import CliRunner


@mock_aws
def test_doctor_json_output_exit_code_zero_and_contains_account(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["--profile", "testprofile", "--output", "json", "doctor"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "Account" in payload


@mock_aws
def test_doctor_json_output_does_not_leak_logs_onto_stdout(cli_runner: CliRunner) -> None:
    # Click 8.2+ always separates stdout/stderr on Result (the old `mix_stderr`
    # CliRunner flag was removed) -- that separation is what makes this assertion
    # meaningful: a log line accidentally printed to stdout would break json.loads.
    result = cli_runner.invoke(
        app, ["--profile", "testprofile", "--verbose", "--output", "json", "doctor"]
    )

    assert result.exit_code == 0, result.output
    json.loads(result.stdout)

"""Smoke tests for the root CLI: version, help, and no-args behavior."""

from aws_admin_cli import __version__
from aws_admin_cli.main import app
from typer.testing import CliRunner


def test_version_flag(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_flag(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "aws-admin-cli" in result.output


def test_no_args_shows_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, [])

    # Typer's no_args_is_help surfaces the help text via a controlled UsageError
    # (exit code 2), not a crash — there must be no unrelated/unhandled exception.
    assert isinstance(result.exception, SystemExit) or result.exception is None
    assert "Usage" in result.output

"""Hybrid-mode entry-point tests: the CLI's non-interactive behavior must be unchanged,
and the TUI must only ever be reached the ways ``main.py`` documents.

``CliRunner`` never presents a TTY on either stream (Click replaces
stdin/stdout with in-memory buffers for every invocation), so every test
here that does NOT pass ``--interactive`` exercises exactly the same branch
a real non-interactive/scripted/CI invocation would take -- there is no
special-casing needed to "simulate" a redirected pipe, it already is one.
"""

import sys

import pytest
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.main import app
from moto import mock_aws
from typer.testing import CliRunner

_BASE_ARGS = ["--profile", "testprofile"]
_TUI_APP_MODULE = "aws_admin_cli.presentation.tui.app"


def test_no_args_exits_2_and_prints_help(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, [])

    assert result.exit_code == 2
    assert "Usage" in result.output


def test_no_args_with_no_interactive_env_also_exits_2(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_ADMIN_CLI_NO_INTERACTIVE", "1")

    result = cli_runner.invoke(app, [])

    assert result.exit_code == 2
    assert "Usage" in result.output


def test_help_mentions_the_interactive_flag(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--interactive" in result.output


@mock_aws
def test_s3_bucket_list_subcommand_is_unaffected_by_the_hybrid_entry_changes(
    cli_runner: CliRunner,
) -> None:
    """A normal subcommand invocation still runs exactly as before Fase 7's main.py edits."""
    create_result = cli_runner.invoke(app, [*_BASE_ARGS, "s3", "bucket", "create", "demo-bucket"])
    assert create_result.exit_code == 0, create_result.output

    list_result = cli_runner.invoke(app, [*_BASE_ARGS, "s3", "bucket", "list"])
    assert list_result.exit_code == 0, list_result.output
    assert "demo-bucket" in list_result.output


def test_explicit_interactive_flag_wins_over_no_interactive_env(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented precedence: ``--interactive`` is checked first (``interactive or
    _tui_available()`` short-circuits), so it launches the TUI even when
    ``AWS_ADMIN_CLI_NO_INTERACTIVE`` is also set -- the env var only ever
    suppresses the IMPLICIT (bare-invocation) TUI launch, never an explicit
    ``--interactive``.
    """
    monkeypatch.setenv("AWS_ADMIN_CLI_NO_INTERACTIVE", "1")
    calls: list[AppContext] = []

    def _stub_run_interactive(ctx: AppContext) -> int:
        calls.append(ctx)
        return 0

    monkeypatch.setattr(f"{_TUI_APP_MODULE}.run_interactive", _stub_run_interactive)

    result = cli_runner.invoke(app, [*_BASE_ARGS, "--interactive"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_version_and_help_never_import_the_tui_app_module(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TUI import inside ``main()`` is lazy: a scripted invocation never pays for it.

    Removes any pre-existing ``sys.modules`` entry first (another test in
    this session may have already imported it) so the assertion is about
    THIS invocation, not accumulated import state from elsewhere in the
    suite; ``monkeypatch`` restores whatever was there afterwards.
    """
    monkeypatch.delitem(sys.modules, _TUI_APP_MODULE, raising=False)
    result = cli_runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert _TUI_APP_MODULE not in sys.modules

    monkeypatch.delitem(sys.modules, _TUI_APP_MODULE, raising=False)
    result = cli_runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert _TUI_APP_MODULE not in sys.modules


def test_no_args_non_tty_never_imports_the_tui_app_module(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, _TUI_APP_MODULE, raising=False)

    result = cli_runner.invoke(app, [])

    assert result.exit_code == 2
    assert _TUI_APP_MODULE not in sys.modules

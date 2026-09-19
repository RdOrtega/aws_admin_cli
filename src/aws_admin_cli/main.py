"""Root Typer application for aws-admin-cli."""

import os
import sys

import typer
from rich.console import Console

from aws_admin_cli import __version__
from aws_admin_cli.core.exceptions import AwsAdminCliError
from aws_admin_cli.presentation.cli.callbacks import (
    EndpointUrlOption,
    InteractiveOption,
    OutputOption,
    ProfileOption,
    QuietOption,
    RegionOption,
    VerboseOption,
    main_callback,
)
from aws_admin_cli.presentation.cli.diagnostics_app import config_command, doctor_command
from aws_admin_cli.presentation.cli.ec2_app import ec2_app
from aws_admin_cli.presentation.cli.iam_app import iam_app
from aws_admin_cli.presentation.cli.s3_app import s3_app
from aws_admin_cli.presentation.cli.stack_app import stack_app
from aws_admin_cli.presentation.cli.vpc_app import vpc_app

app = typer.Typer(
    name="aws-admin-cli",
    help="A CLI for administering AWS (and LocalStack) resources: IAM, S3, VPC, EC2, Stack.",
    # False, not the Typer default: a bare invocation must reach `main()` below (not have
    # Click print help and exit before the callback ever runs), because it's `main()` that
    # decides between launching the TUI and printing help -- see `_tui_available()`.
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
)

# Independent of any AppContext: this is the console the top-level error handler in
# run() falls back on, including for failures (e.g. bad config) that happen before an
# AppContext could ever be built.
_err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"aws-admin-cli {__version__}")
        raise typer.Exit()


def _tui_available() -> bool:
    """Whether a bare invocation (no subcommand, no ``--interactive``) should launch the TUI.

    ``AWS_ADMIN_CLI_NO_INTERACTIVE`` always wins (scripts/CI running in a
    pseudo-TTY, or a user who just wants the old "print help" behavior back,
    can set it without needing ``--interactive`` to be absent too). Otherwise
    it's a real TTY on both ends: launching an arrow-key menu against a
    redirected stdin/stdout would just hang or emit garbage.
    """
    if os.environ.get("AWS_ADMIN_CLI_NO_INTERACTIVE"):
        return False
    return sys.stdin.isatty() and sys.stdout.isatty()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    _version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the application version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
    interactive: InteractiveOption = False,
    profile: ProfileOption = None,
    region: RegionOption = None,
    endpoint_url: EndpointUrlOption = None,
    output: OutputOption = None,
    verbose: VerboseOption = False,
    quiet: QuietOption = False,
) -> None:
    """Administer AWS (and LocalStack) resources from the command line."""
    main_callback(
        ctx,
        profile=profile,
        region=region,
        endpoint_url=endpoint_url,
        output=output,
        verbose=verbose,
        quiet=quiet,
    )

    if ctx.resilient_parsing:
        return
    if ctx.invoked_subcommand is not None:
        return
    if interactive or _tui_available():
        # Lazy: importing presentation.tui.app pulls in questionary/prompt_toolkit,
        # which every scripted/non-interactive invocation (the overwhelming majority)
        # has no reason to pay for.
        from aws_admin_cli.presentation.tui.app import run_interactive

        raise typer.Exit(run_interactive(ctx.obj))
    typer.echo(ctx.get_help())
    raise typer.Exit(2)


# `config` and `doctor` live at the CLI root (`aws-admin-cli doctor`, not
# `aws-admin-cli diagnostics doctor`) — see diagnostics_app.py for their implementation.
app.command("config")(config_command)
app.command("doctor")(doctor_command)
app.add_typer(iam_app, name="iam")
app.add_typer(s3_app, name="s3")
app.add_typer(vpc_app, name="vpc")
app.add_typer(ec2_app, name="ec2")
app.add_typer(stack_app, name="stack")


def run() -> None:
    """Entry point with global error handling.

    Note:
        Deliberately uses ``sys.exit`` rather than ``typer.Exit`` here: by the
        time an ``AwsAdminCliError`` reaches this function, it has already
        propagated past Click's own exception handling inside ``app()`` (Click
        only intercepts its own ``ClickException``/``Exit``, not arbitrary
        exceptions), so ``typer.Exit`` raised at this point would no longer be
        inside a Click command's callback chain to catch it — it would surface
        as a raw, uncaught exception instead of setting the process exit code.
    """
    debug_traceback = os.environ.get("AWS_ADMIN_CLI_DEBUG_TRACEBACK") == "1"
    try:
        app()
    except AwsAdminCliError as exc:
        if debug_traceback:
            raise
        _err_console.print(f"[bold red]Error:[/] {exc}")
        if exc.hint:
            _err_console.print(f"[yellow]Sugerencia:[/] {exc.hint}")
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        if debug_traceback:
            raise
        _err_console.print("[yellow]Cancelado por el usuario.[/]")
        sys.exit(130)

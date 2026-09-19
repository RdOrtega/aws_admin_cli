"""Confirmation prompts guarding destructive CLI operations."""

import sys

import typer
from rich.prompt import Confirm

from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.exceptions import ValidationError


def confirm_destructive(ctx: AppContext, *, action: str, target: str, assume_yes: bool) -> None:
    """Guard a destructive operation behind an explicit confirmation.

    Args:
        ctx: Application context; the prompt is printed through
            ``ctx.err_console`` (STDERR), so it never lands on STDOUT even if
            the command is running with ``--output json``.
        action: What's about to happen, e.g. ``"borrar"``.
        target: What it's about to happen to, e.g. ``"el usuario 'demo'"``.
        assume_yes: Whether ``--yes`` was passed; skips the prompt entirely.

    Raises:
        ValidationError: Not connected to an interactive terminal and
            ``--yes`` wasn't passed -- this must never hang waiting for input
            in a script or CI environment.
        typer.Abort: The user answered "no" at the prompt.
    """
    if assume_yes:
        return

    if not sys.stdin.isatty():
        raise ValidationError(
            f"Se requiere confirmación para {action} {target}, pero no hay una "
            "terminal interactiva.",
            hint="Usa --yes en entornos no interactivos (scripts, CI).",
        )

    confirmed = Confirm.ask(
        f"¿Seguro que quieres {action} {target}?", console=ctx.err_console, default=False
    )
    if not confirmed:
        raise typer.Abort()

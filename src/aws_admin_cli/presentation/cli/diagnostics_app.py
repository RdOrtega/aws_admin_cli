"""Diagnostics commands: ``config`` (no AWS calls) and ``doctor`` (calls STS).

``config_payload``/``doctor_payload`` are the reusable core of each command
(settings introspection / one STS call). They are no longer wired into the
TUI's main menu (that menu was simplified to S3/IAM/EC2 only), but stay
available as ``aws-admin-cli config``/``aws-admin-cli doctor``.

``resolve_current_user`` is a separate, much cheaper scaffold: the TUI
banner's "User" field needs a fast, no-network answer on every redraw, so it
never calls STS itself -- ``doctor_payload``'s ``sts:GetCallerIdentity`` call
is the natural piece to wire in here once a real-AWS resolution is needed.
"""

from typing import Annotated, Any

import typer

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.error_mapper import aws_error_boundary
from aws_admin_cli.presentation.cli.callbacks import OutputOption, apply_output_override
from aws_admin_cli.presentation.cli.formatters.render import render

diagnostics_app = typer.Typer(help="Diagnóstico y configuración.")


def target_label(settings: Settings) -> str:
    """Human-readable label for what ``settings`` points at: LocalStack, or real AWS."""
    return (
        f"LocalStack ({settings.endpoint_url})"
        if settings.is_local
        else f"AWS real ({settings.region})"
    )


def config_payload(settings: Settings) -> dict[str, Any]:
    """Build the ``config`` command's payload: the effective settings, no AWS calls."""
    return {
        "profile": settings.profile,
        "region": settings.region,
        "target": target_label(settings),
        "output": settings.output.value,
        "log_level": settings.log_level.value,
        "max_attempts": settings.max_attempts,
        "retry_mode": settings.retry_mode,
        "connect_timeout": settings.connect_timeout,
        "read_timeout": settings.read_timeout,
        "data_dir": str(settings.data_dir),
    }


def resolve_current_user(settings: Settings) -> str:
    """The TUI banner's "User" field.

    Returns the fixed ``"admin"`` LocalStack always runs as, for a local
    endpoint. Real-AWS resolution (via ``sts:GetCallerIdentity``, the same
    call ``doctor_payload`` already makes) is deliberately not wired in yet
    -- probing STS on every main-menu redraw would be a network call unlike
    this function, and identity resolution for a real account deserves its
    own caching/error-handling story, not a copy-paste into a UI helper.
    """
    if settings.is_local:
        return "admin"
    return "N/A"


def doctor_payload(app_ctx: AppContext, *, timeout: int | None) -> dict[str, Any]:
    """Build the ``doctor`` command's payload: one ``sts:GetCallerIdentity`` call.

    The only real AWS call this makes; any failure propagates as the
    corresponding domain exception (already carries the right message/hint)
    -- never caught here.
    """
    settings = app_ctx.settings
    client_factory = app_ctx.client_factory
    if timeout is not None:
        settings = settings.with_overrides(read_timeout=timeout)
        client_factory = ClientFactory(
            session_factory=app_ctx.client_factory.session_factory, settings=settings
        )

    with aws_error_boundary("sts", "GetCallerIdentity"):
        identity = client_factory.sts().get_caller_identity()

    return {
        "Account": identity["Account"],
        "Arn": identity["Arn"],
        "UserId": identity["UserId"],
        "target": target_label(settings),
    }


@diagnostics_app.command("config")
def config_command(ctx: typer.Context, output: OutputOption = None) -> None:
    """Muestra la configuración efectiva. No realiza ninguna llamada a AWS."""
    app_ctx = apply_output_override(ctx.obj, output)
    render(config_payload(app_ctx.settings), ctx=app_ctx, title="Configuración efectiva")


@diagnostics_app.command("doctor")
def doctor_command(
    ctx: typer.Context,
    timeout: Annotated[
        int | None,
        typer.Option(
            "--timeout", help="Acorta el read_timeout (segundos) para esta verificación."
        ),
    ] = None,
    output: OutputOption = None,
) -> None:
    """Verifica el circuito completo: credenciales, red y el endpoint configurado.

    La única llamada real a AWS permitida en Fase 1: ``sts:GetCallerIdentity``.
    Cualquier fallo se deja propagar como la excepción de dominio correspondiente
    (ya trae el mensaje y el hint correctos) — no se captura aquí.
    """
    app_ctx = apply_output_override(ctx.obj, output)
    render(doctor_payload(app_ctx, timeout=timeout), ctx=app_ctx, title="doctor")

"""Global CLI options, layered on top of environment-resolved ``Settings``.

``main_callback`` is the other half (besides ``Settings.with_overrides``) of the
CLI-flag > environment > .env > default precedence chain: every option below
defaults to ``None`` (or ``False`` for the boolean flags), and only a value the
user actually typed is forwarded to ``with_overrides``, so leaving a flag off
can never shadow whatever ``Settings()`` already resolved from the environment
or ``.env`` file. Environment-variable resolution belongs entirely to
``pydantic-settings`` (see ``aws_admin_cli.core.config``) — options here never
set ``envvar=`` themselves, to avoid two components racing to read the same
``AWS_ADMIN_CLI_*`` variable.
"""

from dataclasses import replace
from typing import Annotated

import typer

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.enums import LogLevel, OutputFormat
from aws_admin_cli.core.exceptions import ValidationError

ProfileOption = Annotated[
    str | None,
    typer.Option("--profile", "-p", help="Perfil de AWS a usar (env AWS_ADMIN_CLI_PROFILE)."),
]
RegionOption = Annotated[
    str | None,
    typer.Option("--region", "-r", help="Región de AWS a usar (env AWS_ADMIN_CLI_REGION)."),
]
EndpointUrlOption = Annotated[
    str | None,
    typer.Option(
        "--endpoint-url",
        help="Endpoint alternativo, p. ej. LocalStack (env AWS_ADMIN_CLI_ENDPOINT_URL).",
    ),
]
OutputOption = Annotated[
    OutputFormat | None,
    typer.Option("--output", "-o", help="Formato de salida (env AWS_ADMIN_CLI_OUTPUT)."),
]
VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="Nivel de log DEBUG.")]
QuietOption = Annotated[bool, typer.Option("--quiet", "-q", help="Nivel de log ERROR.")]
InteractiveOption = Annotated[
    bool,
    typer.Option(
        "--interactive",
        "-i",
        help="Fuerza el modo interactivo (menús navegables), incluso sin TTY.",
    ),
]


def apply_output_override(app_ctx: AppContext, output: OutputFormat | None) -> AppContext:
    """Layer a command-level ``--output`` on top of the root-level ``AppContext``.

    ``--output`` (like the other global options) is parsed at the root
    callback, before the subcommand name -- but Click doesn't let a subcommand
    read options declared on its parent, so ``aws-admin-cli config --output
    json`` (flag *after* the subcommand) needs each render-producing command to
    also accept it directly and layer it on top of whatever the root already
    resolved. Every render-producing command follows this same pattern.
    """
    if output is None:
        return app_ctx
    return replace(app_ctx, settings=app_ctx.settings.with_overrides(output=output))


def main_callback(
    ctx: typer.Context,
    profile: ProfileOption = None,
    region: RegionOption = None,
    endpoint_url: EndpointUrlOption = None,
    output: OutputOption = None,
    verbose: VerboseOption = False,
    quiet: QuietOption = False,
) -> None:
    """Resolve ``Settings`` for this invocation and wire an ``AppContext`` onto ``ctx.obj``.

    Args:
        ctx: The active Typer/Click context.
        profile: ``--profile`` override, or ``None`` if not passed.
        region: ``--region`` override, or ``None`` if not passed.
        endpoint_url: ``--endpoint-url`` override, or ``None`` if not passed.
        output: ``--output`` override, or ``None`` if not passed.
        verbose: Whether ``--verbose`` was passed.
        quiet: Whether ``--quiet`` was passed.

    Raises:
        ValidationError: ``--verbose`` and ``--quiet`` were both passed.
    """
    if ctx.resilient_parsing:
        return

    if verbose and quiet:
        raise ValidationError(
            "--verbose y --quiet son mutuamente excluyentes.",
            hint="Usa solo una de las dos opciones.",
        )

    log_level: LogLevel | None = None
    if verbose:
        log_level = LogLevel.DEBUG
    elif quiet:
        log_level = LogLevel.ERROR

    settings = Settings().with_overrides(
        profile=profile,
        region=region,
        endpoint_url=endpoint_url,
        output=output,
        log_level=log_level,
    )

    app_ctx = AppContext.build(settings)
    ctx.obj = app_ctx

    # DEBUG-only, and only the non-secret shape of the target: never log credentials,
    # not even at this level.
    app_ctx.logger.debug(
        "Destino efectivo: perfil=%s región=%s endpoint=%s",
        settings.profile,
        settings.region,
        settings.endpoint_url or "(AWS real)",
    )

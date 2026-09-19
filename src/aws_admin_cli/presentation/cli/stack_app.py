"""Stack CLI commands: validate/plan/apply/list/show/status/destroy a manifest (Fase 6).

Same shape as every other ``*_app.py``: pull ``AppContext`` off ``ctx.obj``,
build gateways/use cases/the engine, call it, hand the result to ``render()``.
The one thing unique to this module is ``on_event``: ``StackEngine.apply``/
``destroy`` emit progress notifications the engine itself knows nothing
about rendering -- ``_progress_printer`` below is the only place that turns
those into Rich output on ``ctx.err_console``.
"""

from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from aws_admin_cli.application.stacks.engine import StackEvent, StackPlan
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.enums import OutputFormat
from aws_admin_cli.core.exceptions import StackApplyError
from aws_admin_cli.domain.models.stack import (
    ResourceState,
    ResourceStatus,
    StackManifest,
    StackState,
)
from aws_admin_cli.infrastructure.manifests.yaml_loader import load_manifest
from aws_admin_cli.presentation.cli.callbacks import OutputOption, apply_output_override
from aws_admin_cli.presentation.cli.confirm import confirm_destructive
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.wiring import build_stack_engine

stack_app = typer.Typer(help="Motor de orquestación declarativo (manifiestos de stack).")

YesOption = Annotated[bool, typer.Option("--yes", help="No pedir confirmación.")]

_STATUS_STYLE: dict[ResourceStatus, str] = {
    ResourceStatus.CREATING: "yellow",
    ResourceStatus.CREATED: "green",
    ResourceStatus.SKIPPED: "dim",
    ResourceStatus.FAILED: "bold red",
    ResourceStatus.COMPENSATING: "yellow",
    ResourceStatus.COMPENSATED: "green",
    ResourceStatus.COMPENSATION_FAILED: "bold red",
    ResourceStatus.DESTROYED: "green",
}


# -- Composition -----------------------------------------------------------------------


def _noop_on_event(event: StackEvent) -> None:
    del event


def _progress_printer(app_ctx: AppContext, console: Console) -> Any:
    """Build an ``on_event`` callback that prints progress to ``console`` -- or a no-op.

    Disabled (no-op) in JSON output mode or without a TTY, same rule every
    other progress indicator in this CLI follows.
    """
    if app_ctx.output is OutputFormat.JSON or not console.is_terminal:
        return _noop_on_event

    def _print(event: StackEvent) -> None:
        style = _STATUS_STYLE.get(event.status, "")
        phase, kind, status = event.phase, event.kind.value, event.status.value
        line = f"[{style}][{phase}][/] {event.logical_id} ({kind}): {status}"
        if event.detail:
            line += f" -- {event.detail}"
        console.print(line)

    return _print


# -- Rendering ---------------------------------------------------------------------------


def _plan_rows(plan: StackPlan) -> list[dict[str, Any]]:
    return [
        {
            "Order": resource.order,
            "LogicalId": resource.logical_id,
            "Kind": resource.kind.value,
            "Action": resource.action.value.upper(),
            "DependsOn": list(resource.depends_on),
        }
        for resource in plan.resources
    ]


def _render_plan(plan: StackPlan, *, ctx: AppContext, output: OutputFormat | None) -> None:
    app_ctx = apply_output_override(ctx, output)
    render(_plan_rows(plan), ctx=app_ctx, title=f"stack plan: {plan.stack_name}")


def _resource_row(resource: ResourceState) -> dict[str, Any]:
    return {
        "LogicalId": resource.logical_id,
        "Kind": resource.kind.value,
        "Status": resource.status.value,
        "PhysicalId": resource.physical_id,
        "ManagedByStack": resource.created_by_stack,
        "Error": resource.error,
    }


def _render_stack_state(
    state: StackState, *, ctx: AppContext, output: OutputFormat | None, title: str
) -> None:
    app_ctx = apply_output_override(ctx, output)
    if app_ctx.output is OutputFormat.JSON:
        render(state.model_dump(mode="json"), ctx=app_ctx, title=title)
        return
    summary = {
        "Name": state.name,
        "Status": state.status.value,
        "Profile": state.profile,
        "Region": state.region,
        "UpdatedAt": state.updated_at.isoformat(),
    }
    app_ctx.console.print(f"[bold]{title}[/]")
    render(summary, ctx=app_ctx)
    render([_resource_row(r) for r in state.resources], ctx=app_ctx, title="resources")


def _print_apply_failure_breakdown(
    app_ctx: AppContext, stack_name: str, error: StackApplyError
) -> None:
    """Print the three-way breakdown a failed apply needs: failed, cleaned, orphaned.

    Always to STDERR, regardless of ``--output`` -- diagnostic, never part
    of a JSON payload a script might parse from STDOUT.
    """
    console = app_ctx.err_console
    console.print(f"[bold red]El apply falló:[/] {error.message}")
    if error.orphaned:
        console.print(
            f"[bold red]{len(error.orphaned)} recurso(s) huérfano(s)[/] -- "
            "el rollback NO pudo limpiarlos, siguen en AWS:"
        )
        for resource in error.orphaned:
            console.print(
                f"  [bold red]✗[/] {resource.logical_id} ({resource.kind.value}) "
                f"physical_id={resource.physical_id}"
            )
        console.print(
            f"[yellow]Sugerencia:[/] revisa `stack status {stack_name}` y, cuando el error "
            f"original esté resuelto, reintenta `stack apply`; o límpialos a mano si el "
            "manifiesto ya no aplica."
        )
    else:
        console.print("[green]El rollback limpió todo lo que este apply había creado.[/]")


# -- Commands -----------------------------------------------------------------------------


@stack_app.command("validate")
def stack_validate(ctx: typer.Context, file: Path) -> None:
    """Valida un manifiesto de stack (sintaxis YAML, ids, dependencias, red prohibida)."""
    app_ctx: AppContext = ctx.obj
    manifest = load_manifest(file)
    app_ctx.console.print(
        f"[green]'{file}' es un manifiesto de stack válido[/] "
        f"('{manifest.name}', {len(manifest.resources)} recurso(s))."
    )


@stack_app.command("plan")
def stack_plan(ctx: typer.Context, file: Path, output: OutputOption = None) -> None:
    """Muestra el plan (orden topológico + acción CREATE/NO-OP/REPLACE) sin tocar nada."""
    app_ctx: AppContext = ctx.obj
    manifest = load_manifest(file)
    engine = build_stack_engine(app_ctx)
    plan = engine.plan(manifest)
    _render_plan(plan, ctx=app_ctx, output=output)


@stack_app.command("apply")
def stack_apply(
    ctx: typer.Context,
    file: Path,
    yes: YesOption = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    no_rollback: Annotated[
        bool,
        typer.Option(
            "--no-rollback", help="Si falla, deja los recursos creados sin compensar."
        ),
    ] = False,
    output: OutputOption = None,
) -> None:
    """Aplica un manifiesto de stack: crea/actualiza sus recursos en orden.

    Sin --yes, muestra el plan y pide confirmación antes de tocar nada
    (--dry-run nunca toca nada, así que nunca pide confirmación).
    """
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    manifest: StackManifest = load_manifest(file)
    engine = build_stack_engine(app_ctx)

    if not dry_run:
        plan = engine.plan(manifest)
        _render_plan(plan, ctx=app_ctx_out, output=None)
        changed = sum(1 for r in plan.resources if r.action.value != "no-op")
        confirm_destructive(
            app_ctx,
            action="aplicar",
            target=f"el stack '{manifest.name}' ({changed} cambio(s) de {len(plan.resources)} "
            "recurso(s))",
            assume_yes=yes,
        )

    on_event = _progress_printer(app_ctx_out, app_ctx_out.err_console)
    try:
        state = engine.apply(manifest, dry_run=dry_run, no_rollback=no_rollback, on_event=on_event)
    except StackApplyError as exc:
        _print_apply_failure_breakdown(app_ctx_out, manifest.name, exc)
        raise

    _render_stack_state(state, ctx=app_ctx_out, output=None, title="stack apply")


@stack_app.command("list")
def stack_list(ctx: typer.Context, output: OutputOption = None) -> None:
    """Lista los stacks con estado persistido."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    states = app_ctx.stack_repository.list_all()
    if app_ctx_out.output is OutputFormat.JSON:
        render([s.model_dump(mode="json") for s in states], ctx=app_ctx_out, title="stack list")
        return
    rows = [
        {
            "Name": s.name,
            "Status": s.status.value,
            "Resources": len(s.resources),
            "Profile": s.profile,
            "Region": s.region,
            "UpdatedAt": s.updated_at.isoformat(),
        }
        for s in states
    ]
    render(rows, ctx=app_ctx_out, title="stack list")


@stack_app.command("show")
def stack_show(ctx: typer.Context, name: str, output: OutputOption = None) -> None:
    """Muestra el estado persistido de un stack."""
    app_ctx: AppContext = ctx.obj
    engine = build_stack_engine(app_ctx)
    state = engine.status(name, refresh=False)
    _render_stack_state(state, ctx=app_ctx, output=output, title="stack show")


@stack_app.command("status")
def stack_status(
    ctx: typer.Context,
    name: str,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Comprueba drift contra AWS en tiempo real.")
    ] = False,
    output: OutputOption = None,
) -> None:
    """Muestra el estado de un stack; --refresh comprueba drift contra AWS en vivo."""
    app_ctx: AppContext = ctx.obj
    engine = build_stack_engine(app_ctx)
    state = engine.status(name, refresh=refresh)
    _render_stack_state(state, ctx=app_ctx, output=output, title="stack status")


@stack_app.command("destroy")
def stack_destroy(
    ctx: typer.Context,
    name: str,
    yes: YesOption = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Continúa aunque una compensación falle, en vez de detenerse."
        ),
    ] = False,
    output: OutputOption = None,
) -> None:
    """Destruye todo lo que este stack creó (nunca lo que solo reutilizó), en orden inverso."""
    app_ctx: AppContext = ctx.obj
    app_ctx_out = apply_output_override(app_ctx, output)
    engine = build_stack_engine(app_ctx)

    current = engine.status(name, refresh=False)
    managed = sum(1 for r in current.resources if r.created_by_stack)
    confirm_destructive(
        app_ctx,
        action="destruir",
        target=f"el stack '{name}' ({managed} recurso(s) gestionados por este stack)",
        assume_yes=yes,
    )

    on_event = _progress_printer(app_ctx_out, app_ctx_out.err_console)
    state = engine.destroy(name, force=force, on_event=on_event)
    _render_stack_state(state, ctx=app_ctx_out, output=None, title="stack destroy")

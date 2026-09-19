"""The Stack TUI screen: plan/apply/status/destroy a declarative manifest.

``_on_event`` mirrors ``presentation/cli/stack_app.py``'s ``_progress_printer``
(same status->color mapping), printed live to ``err_console`` as
``StackEngine.apply``/``destroy`` progresses. A failed apply shows the same
three-way failed/cleaned/orphaned breakdown the CLI's
``_print_apply_failure_breakdown`` does -- a TUI user needs exactly the same
"what do I need to clean up by hand" answer a CLI user does.

Not currently reachable from the TUI's main menu (which was simplified to
S3/IAM/EC2 only) -- it stays fully implemented, and remains reachable
through the non-interactive CLI (``aws-admin-cli stack ...``).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Self

from aws_admin_cli.application.stacks.engine import StackEvent
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.exceptions import StackApplyError
from aws_admin_cli.domain.models.stack import ResourceStatus, StackManifest
from aws_admin_cli.infrastructure.manifests.yaml_loader import load_manifest
from aws_admin_cli.presentation.cli.formatters.render import render
from aws_admin_cli.presentation.tui.flows._shared import confirm_yes_no
from aws_admin_cli.presentation.tui.menu import (
    NAV_BACK,
    NAV_EXIT,
    Choice,
    Separator,
    back_and_exit_choices,
)
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter
from aws_admin_cli.presentation.wiring import build_stack_engine

__all__ = ["StackFlow"]

_PLAN = "plan"
_APPLY = "apply"
_STATUS = "status"
_DESTROY = "destroy"

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


@dataclass(slots=True)
class StackFlow:
    """Stack's top-level TUI screen: plan, apply, status, and destroy."""

    title: ClassVar[str] = "Stack (orchestration engine)"

    ctx: AppContext
    prompter: Prompter

    def menu(self: Self) -> NavAction:
        """Show Stack's menu once."""
        selected = self.prompter.select("Stack -- what do you want to do?", self._choices())
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected == NAV_BACK:
            return NavAction.BACK
        {
            _PLAN: self._plan,
            _APPLY: self._apply,
            _STATUS: self._status,
            _DESTROY: self._destroy,
        }[selected]()
        return NavAction.STAY

    def _choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(title="Plan (dry-run)", value=_PLAN),
            Choice(title="Apply", value=_APPLY),
            Choice(title="Status", value=_STATUS),
            Choice(title="Destroy", value=_DESTROY),
            *back_and_exit_choices(),
        ]

    def _on_event(self: Self, event: StackEvent) -> None:
        style = _STATUS_STYLE.get(event.status, "")
        line = (
            f"[{style}][{event.phase}][/] {event.logical_id} ({event.kind.value}): "
            f"{event.status.value}"
        )
        if event.detail:
            line += f" -- {event.detail}"
        self.ctx.err_console.print(line)

    def _load_manifest(self: Self, message: str) -> StackManifest | None:
        raw_path = self.prompter.path(message)
        if not raw_path:
            return None
        manifest: StackManifest = load_manifest(Path(raw_path))
        return manifest

    def _plan(self: Self) -> None:
        manifest = self._load_manifest("Manifest path:")
        if manifest is None:
            return
        engine = build_stack_engine(self.ctx)
        plan = engine.plan(manifest)
        render(
            [
                {
                    "Order": r.order,
                    "LogicalId": r.logical_id,
                    "Kind": r.kind.value,
                    "Action": r.action.value.upper(),
                }
                for r in plan.resources
            ],
            ctx=self.ctx,
            title=f"stack plan: {plan.stack_name}",
        )
        self.prompter.pause()

    def _apply(self: Self) -> None:
        manifest = self._load_manifest("Manifest path:")
        if manifest is None:
            return
        engine = build_stack_engine(self.ctx)
        plan = engine.plan(manifest)
        changed = sum(1 for r in plan.resources if r.action.value != "no-op")
        if not confirm_yes_no(
            self.prompter,
            f"Apply '{manifest.name}' ({changed} change(s) out of {len(plan.resources)} "
            "resource(s))?",
        ):
            return

        try:
            state = engine.apply(manifest, on_event=self._on_event)
        except StackApplyError as exc:
            self.ctx.err_console.print(f"[bold red]The apply failed:[/] {exc.message}")
            if exc.orphaned:
                self.ctx.err_console.print(
                    f"[bold red]{len(exc.orphaned)} orphaned resource(s)[/] -- the rollback "
                    "could NOT clean them up, they are still in AWS."
                )
            else:
                self.ctx.err_console.print(
                    "[green]The rollback cleaned up everything this apply had created.[/]"
                )
            self.prompter.pause()
            return

        self.ctx.console.print(f"[green]Stack '{state.name}' applied ({state.status.value}).[/]")
        self.prompter.pause()

    def _pick_stack(self: Self, message: str) -> str | None:
        states = self.ctx.stack_repository.list_all()
        if not states:
            self.ctx.err_console.print("[yellow]No stacks with persisted state.[/]")
            self.prompter.pause()
            return None
        choices: list[Choice | Separator] = [
            Choice(title=f"{s.name} ({s.status.value})", value=s.name) for s in states
        ]
        choices.append(Separator())
        choices.append(Choice(title="<- Cancel", value=NAV_BACK))
        selected = self.prompter.select(message, choices)
        if selected is None or selected == NAV_BACK:
            return None
        return selected

    def _status(self: Self) -> None:
        name = self._pick_stack("Which stack do you want to inspect?")
        if name is None:
            return
        engine = build_stack_engine(self.ctx)
        state = engine.status(name, refresh=False)
        render(
            {
                "Name": state.name,
                "Status": state.status.value,
                "Profile": state.profile,
                "Region": state.region,
                "UpdatedAt": state.updated_at.isoformat(),
            },
            ctx=self.ctx,
            title="stack status",
        )
        self.prompter.pause()

    def _destroy(self: Self) -> None:
        name = self._pick_stack("Which stack do you want to destroy?")
        if name is None:
            return
        engine = build_stack_engine(self.ctx)
        current = engine.status(name, refresh=False)
        managed = sum(1 for r in current.resources if r.created_by_stack)
        if not confirm_yes_no(
            self.prompter, f"Destroy '{name}' ({managed} resource(s) managed by this stack)?"
        ):
            return
        state = engine.destroy(name, on_event=self._on_event)
        self.ctx.console.print(f"[green]Stack '{name}' destroyed ({state.status.value}).[/]")
        self.prompter.pause()

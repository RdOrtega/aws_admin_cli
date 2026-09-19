"""``LambdaFlow``: basic AWS Lambda & Serverless scaffolding screen.

Read-only, and deliberately small: list functions across every AWS region
(the same global, parallel multi-region scan pattern ``ec2_flow.py``'s
"Search / Filter Instances" uses), then view one function's runtime, handler,
and environment variables. Talks to AWS exclusively through
``presentation.wiring.build_lambda_service(ctx)`` -- never ``boto3``/
``botocore`` directly, same boundary every other flow in this package holds
(see ``tests/unit/architecture/test_tui_boundaries.py``).

No creation/mutation here at all, so the active session/header region
(``ctx.settings.region``) never enters into anything this flow does -- it
stays reserved for resource-creation workflows elsewhere (EC2 launch, S3
bucket creation), exactly as every other read-only scan in this project
already respects.
"""

from dataclasses import dataclass
from typing import ClassVar, Self

from rich.table import Table

from aws_admin_cli.core.context import AppContext
from aws_admin_cli.infrastructure.aws.lambda_service import (
    LambdaFunctionDetail,
    LambdaFunctionSummary,
    LambdaService,
)
from aws_admin_cli.presentation.tui.flows._shared import clear_and_banner, run_with_spinner
from aws_admin_cli.presentation.tui.flows.error_handler import aws_error_handler
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice, Separator
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter
from aws_admin_cli.presentation.wiring import build_lambda_service

__all__ = ["LambdaFlow"]

_LIST_FUNCTIONS = "list_functions"


def _functions_table(functions: list[LambdaFunctionSummary]) -> Table:
    table = Table(title="Lambda Functions (All Regions)")
    table.add_column("Function Name", style="bold")
    table.add_column("Runtime")
    table.add_column("Region")
    table.add_column("Memory (MB)", justify="right")
    table.add_column("Timeout (s)", justify="right")
    for fn in functions:
        table.add_row(
            fn.function_name,
            fn.runtime,
            fn.region,
            str(fn.memory_size_mb) if fn.memory_size_mb is not None else "-",
            str(fn.timeout_s) if fn.timeout_s is not None else "-",
        )
    return table


def _detail_table(detail: LambdaFunctionDetail) -> Table:
    table = Table(title=f"Function: {detail.function_name}", show_header=False)
    table.add_column("Property", style="bold cyan")
    table.add_column("Value")
    table.add_row("Function Name", detail.function_name)
    table.add_row("Region", detail.region)
    table.add_row("Runtime", detail.runtime)
    table.add_row("Handler", detail.handler or "(none)")
    table.add_row("Memory (MB)", str(detail.memory_size_mb) if detail.memory_size_mb else "-")
    table.add_row("Timeout (s)", str(detail.timeout_s) if detail.timeout_s else "-")
    env_lines = "\n".join(f"{k}={v}" for k, v in detail.environment.items())
    table.add_row("Environment Variables", env_lines or "(none)")
    return table


@dataclass(slots=True)
class LambdaFlow:
    """Root screen for AWS Lambda & Serverless: list functions, view configuration."""

    title: ClassVar[str] = "⚡ AWS Lambda & Serverless"

    ctx: AppContext
    prompter: Prompter

    @aws_error_handler
    def menu(self: Self) -> NavAction:
        """Show the Lambda root menu once."""
        clear_and_banner(self.ctx)
        selected = self.prompter.select(
            "AWS Lambda & Serverless -- what do you want to do?", self._choices()
        )
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected == NAV_BACK:
            return NavAction.BACK
        if selected == _LIST_FUNCTIONS:
            self._list_functions_loop()
        return NavAction.STAY

    def _choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(title="🔎 List Functions (All Regions)", value=_LIST_FUNCTIONS),
            Separator(),
            Choice(title="↩️  Back to Main Menu", value=NAV_BACK),
        ]

    def _list_functions_loop(self: Self) -> None:
        """List every function across every region -> pick one -> view its detail."""
        service = build_lambda_service(self.ctx)
        while True:
            clear_and_banner(self.ctx)
            functions = run_with_spinner(
                self.ctx.err_console,
                "[bold green]Scanning Lambda functions across every region...[/bold green]",
                service.list_functions_all_regions,
            )
            self.ctx.console.print(_functions_table(functions))
            if not functions:
                self.ctx.err_console.print("[yellow]No Lambda functions found.[/]")
                self.prompter.pause()
                return

            choices: list[Choice | Separator] = [
                Choice(title=f"{fn.function_name}  [{fn.region}]", value=fn.function_name)
                for fn in functions
            ]
            choices.append(Separator())
            choices.append(Choice(title="↩️  Back", value=NAV_BACK))
            selected = self.prompter.select(
                "Select a function to view its configuration:", choices
            )
            if selected is None or selected == NAV_BACK:
                return
            fn = next(f for f in functions if f.function_name == selected)
            self._render_function_detail(service, fn)

    def _render_function_detail(
        self: Self, service: LambdaService, fn: LambdaFunctionSummary
    ) -> None:
        clear_and_banner(self.ctx)
        detail = run_with_spinner(
            self.ctx.err_console,
            f"[bold green]Fetching configuration for '{fn.function_name}'...[/bold green]",
            lambda: service.get_function(fn.function_name, region=fn.region),
        )
        self.ctx.console.print(_detail_table(detail))
        self.prompter.pause()

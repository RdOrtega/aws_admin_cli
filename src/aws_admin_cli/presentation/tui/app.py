"""The interactive TUI's entry point.

``run_interactive(ctx) -> int`` is the one thing ``main.py`` calls (lazily,
so a normal scripted invocation never even imports ``questionary``): it
builds the root menu and drives it with a ``NavigationStack`` until the user
exits. The root menu opens with the "Deep Enterprise" Governance Grid header
(title, Target/Region/User/Status) on every redraw, plus a single red line
when the session targets real AWS -- a safety signal, not decoration.
"""

from dataclasses import dataclass, field
from typing import ClassVar, Self

from aws_admin_cli.core.context import AppContext
from aws_admin_cli.infrastructure.aws.aws_client import aws_connection_is_healthy
from aws_admin_cli.presentation.cli.diagnostics_app import target_label
from aws_admin_cli.presentation.tui.flows._shared import render_header, resolve_endpoint_status
from aws_admin_cli.presentation.tui.flows.audit_flow import AuditFlow
from aws_admin_cli.presentation.tui.flows.base import Flow
from aws_admin_cli.presentation.tui.flows.cloudwatch_flow import CloudWatchFlow
from aws_admin_cli.presentation.tui.flows.ec2_flow import Ec2Flow
from aws_admin_cli.presentation.tui.flows.environment_flow import EnvironmentFlow
from aws_admin_cli.presentation.tui.flows.iam_flow import IamFlow
from aws_admin_cli.presentation.tui.flows.lambda_flow import LambdaFlow
from aws_admin_cli.presentation.tui.flows.s3_flow import S3Flow
from aws_admin_cli.presentation.tui.menu import NAV_EXIT, Choice, Separator
from aws_admin_cli.presentation.tui.navigation import NavAction, NavigationStack, clear_terminal
from aws_admin_cli.presentation.tui.prompter import Prompter, QuestionaryPrompter

__all__ = ["run_interactive"]

# The main menu's curated set, in the exact order the product spec requires:
# Environment, IAM, EC2, Lambda, S3, CloudWatch, Audit. `EnvironmentFlow` is TUI-only
# chrome (no CLI command, no AWS calls -- see its own module docstring), so it stays out
# of `flows/registry.py`'s catalog (which lists exactly the five domain flows this
# project built -- see `tests/unit/architecture/test_tui_boundaries.py`). Same reasoning
# keeps `AuditFlow`, `CloudWatchFlow`, and `LambdaFlow` out of that registry too: each is
# a cross-cutting reporting/scaffolding screen, not one of that catalog's five domains.
# VPC and Stack are deliberately left off the interactive main screen (product
# decision), but stay fully implemented and reachable via the non-interactive CLI
# (`aws-admin-cli vpc ...` / `aws-admin-cli stack ...`).
_MAIN_MENU_FLOWS: tuple[type[Flow], ...] = (
    EnvironmentFlow,
    IamFlow,
    Ec2Flow,
    LambdaFlow,
    S3Flow,
    CloudWatchFlow,
    AuditFlow,
)
# Routing keys are the flows' own class names -- never the display ``title`` (which
# carries an icon/label meant purely for the human eye) -- so relabeling a menu entry
# can never silently break which flow gets pushed onto the stack.
_FLOW_BY_KEY: dict[str, type[Flow]] = {
    flow_cls.__name__: flow_cls for flow_cls in _MAIN_MENU_FLOWS
}
# The circuit breaker's scope: every flow whose very first action is a live AWS call
# (IAM/EC2/Lambda/S3/CloudWatch/Audit each render a live read as the first statement of
# ``.menu()``). ``EnvironmentFlow`` is deliberately excluded: it makes no AWS calls at
# all (see its own module docstring) and is the one screen that must stay reachable
# while offline -- it's where an admin would go to switch region or just see status.
_CONNECTIVITY_GATED_FLOWS: frozenset[str] = frozenset(
    {
        IamFlow.__name__,
        Ec2Flow.__name__,
        LambdaFlow.__name__,
        S3Flow.__name__,
        CloudWatchFlow.__name__,
        AuditFlow.__name__,
    }
)

_CONNECTION_ERROR_LINES: tuple[str, ...] = (
    "[!] Connection Error: Unable to reach AWS/LocalStack target endpoint.",
    "[!] Diagnostic: Docker container is inactive or network connection timed out.",
)
_CONNECTION_ERROR_HINT = (
    "[→] Remediation: Ensure LocalStack is running ('docker start localstack') "
    "or verify credentials."
)

_FAREWELL_MESSAGE = "✨ Session closed successfully. See you later, cloud engineer! 🚀"


@dataclass(slots=True)
class _MainMenu:
    """The TUI's root menu: the header, then Environment/IAM/EC2/S3 and Exit.

    ``stack`` is set by ``run_interactive`` right after construction (a
    ``NavigationStack`` needs its root at construction time, so the root
    can't already hold a reference to it) -- the only thing this class ever
    does with it is ``push()`` the one flow the user picked; it's ``None``
    only in the impossible window before that assignment happens.
    """

    title: ClassVar[str] = "aws-admin-cli"

    ctx: AppContext
    prompter: Prompter
    stack: NavigationStack = field(init=False, repr=False)

    def menu(self: Self) -> NavAction:
        """Show the root menu once."""
        self._render_banner()
        self._warn_if_real_aws()
        selected = self.prompter.select("Select Operational Module:", self._choices())
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected in _FLOW_BY_KEY:
            if selected in _CONNECTIVITY_GATED_FLOWS and not aws_connection_is_healthy(self.ctx):
                self._print_connection_error()
                return NavAction.STAY
            self.stack.push(_FLOW_BY_KEY[selected](self.ctx, self.prompter))
        return NavAction.STAY

    def _print_connection_error(self: Self) -> None:
        """The circuit breaker's diagnostic alert: printed, never raised.

        Blocks entry into a connectivity-gated flow (see
        ``_CONNECTIVITY_GATED_FLOWS``) when ``aws_connection_is_healthy``
        reports the target unreachable -- an operator gets this structured,
        actionable message and a clean return to the main menu instead of the
        flow starting anyway and failing on its own first AWS call.
        """
        for line in _CONNECTION_ERROR_LINES:
            self.ctx.err_console.print(f"[red]{line}[/]")
        self.ctx.err_console.print(f"[yellow]{_CONNECTION_ERROR_HINT}[/]")
        self.prompter.pause("Press ENTER to return to the menu...")

    def _render_banner(self: Self) -> None:
        """Print the Governance Grid header: title, then Target/Region/User/Status.

        Delegates to ``flows._shared.render_header`` -- the actual logic
        lives there so nested flow screens (e.g. IAM's user detail view) can
        redraw the same header after a clear, without importing this module
        (which would be circular: ``app.py`` already imports every flow).
        Kept as a method (rather than inlining the free-function call at
        every ``menu()`` site) purely so existing callers/tests of
        ``_MainMenu`` keep working unchanged.
        """
        render_header(self.ctx)

    @staticmethod
    def _resolve_status(endpoint_url: str | None) -> tuple[str, str]:
        """See ``flows._shared.resolve_endpoint_status`` -- same delegation reasoning."""
        return resolve_endpoint_status(endpoint_url)

    def _warn_if_real_aws(self: Self) -> None:
        """Print one red line when this session targets real AWS -- nothing otherwise.

        Silent in the common LocalStack case: the point is to be impossible
        to miss precisely when a mistake would cost money.
        """
        if not self.ctx.settings.is_local:
            self.ctx.err_console.print(
                f"[bold red]AWS REAL -- {target_label(self.ctx.settings)}[/]\n"
            )

    def _choices(self: Self) -> list[Choice | Separator]:
        flow_choices = [
            Choice(title=flow_cls.title, value=flow_cls.__name__) for flow_cls in _MAIN_MENU_FLOWS
        ]
        return [
            *flow_choices,
            Separator(line="─" * 70),
            Choice(title="🚪 Exit CLI Session", value=NAV_EXIT),
        ]


def run_interactive(ctx: AppContext) -> int:
    """Launch the interactive TUI against ``ctx``; returns the process exit code."""
    prompter: Prompter = QuestionaryPrompter()
    main_menu = _MainMenu(ctx=ctx, prompter=prompter)
    stack = NavigationStack(
        root=main_menu, err_console=ctx.err_console, logger=ctx.logger, prompter=prompter
    )
    main_menu.stack = stack
    exit_code = stack.run()
    if exit_code == 0:
        # 0 only ever means "Exit" was selected (from any depth) -- Ctrl+C at the root
        # returns 130 instead, and never reaches here.
        _print_farewell(ctx)
    return exit_code


def _print_farewell(ctx: AppContext) -> None:
    """Clear the screen and say goodbye once navigation ends normally."""
    if ctx.err_console.is_terminal:
        clear_terminal()
    ctx.err_console.print()  # top margin (2 lines): keeps the farewell off the terminal's edge
    ctx.err_console.print()
    ctx.err_console.print(_FAREWELL_MESSAGE)
    ctx.err_console.print()  # trailing blank line: separates the message from the shell prompt

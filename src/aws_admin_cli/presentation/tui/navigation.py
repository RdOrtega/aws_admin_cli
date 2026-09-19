"""Explicit stack-based navigation for the TUI.

``NavigationStack`` owns a plain ``list`` of ``Navigable`` flows (the "back"
history) and drives whichever one is on top with a single ``while`` loop --
never by having one flow's ``.menu()`` call another ``NavigationStack``
recursively. A flow that wants to descend into a child menu calls
``stack.push(child)``; the loop picks the new top up on its very next
iteration. This keeps navigation depth bounded by the stack's own length,
never by the Python call stack, no matter how many times the user drills in
and backs out (see ``tests/unit/presentation/test_navigation.py``'s
50-navigation stress test).

``Navigable`` is a local, minimal ``Protocol`` -- deliberately NOT the
``Flow`` Protocol from ``presentation/tui/flows/base.py``. Protocols are
structural: any real ``Flow`` implementation already satisfies ``Navigable``
(it has ``.menu() -> NavAction``), so this module has zero import dependency
on ``flows/base.py``, and vice versa.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Protocol, Self, runtime_checkable

from aws_admin_cli.core.exceptions import AwsAdminCliError, EndpointUnavailableError

if TYPE_CHECKING:
    import logging

    from rich.console import Console

    from aws_admin_cli.presentation.tui.prompter import Prompter

__all__ = ["NavAction", "Navigable", "NavigationStack", "clear_terminal"]


def clear_terminal() -> None:
    r"""Wipe the terminal with raw ANSI escapes, not the ``clear``/``cls`` binary.

    Shelling out to ``clear`` depends on the terminfo database the terminal
    reports: VS Code's integrated terminal (and some multiplexers) resolve it
    to a sequence that clears the visible viewport but leaves scrollback
    intact, so ``prompt_toolkit``'s next arrow-key repaint of a ``questionary``
    list computes the wrong redraw height and appears to swallow options as
    the user scrolls. Writing the three escapes directly sidesteps terminfo
    entirely: ``\033[H`` homes the cursor, ``\033[2J`` clears the visible
    screen, ``\033[3J`` purges scrollback -- the same three effects, without
    trusting the terminal's ``clear`` binary to have wired them up correctly.
    """
    print("\033[H\033[2J\033[3J", end="", flush=True)


class NavAction(str, Enum):
    """What a flow's ``.menu()`` call wants to happen next.

    STAY: redisplay this same flow's menu (e.g. after completing one action).
    BACK: pop this flow off the stack, returning to whatever is beneath it
        -- popping the last flow on the stack ends navigation (exit code 0),
        so BACK at the root is EXIT in every practical sense.
    EXIT: end navigation immediately, from any depth.
    """

    STAY = "stay"
    BACK = "back"
    EXIT = "exit"


@runtime_checkable
class Navigable(Protocol):
    """The structural shape ``NavigationStack`` needs from a flow.

    ``title`` is ``ClassVar[str]`` (not a plain instance variable) to match
    ``flows/base.py``'s ``Flow.title`` exactly -- mypy's Protocol structural
    matching treats the two as distinct, and a real ``Flow`` subclass (which
    sets ``title`` as a class attribute, per spec) would otherwise fail to
    satisfy this Protocol.
    """

    title: ClassVar[str]

    def menu(self: Self) -> NavAction:
        """Show this flow's menu once; return what navigation should do next."""
        ...


@dataclass(slots=True)
class NavigationStack:
    """Drives a stack of ``Navigable`` flows with an explicit while-loop.

    Args:
        root: The flow shown first (and never poppable past -- popping it
            ends navigation).
        err_console: Where recovered errors and the Ctrl+C notice are
            printed (STDERR -- same convention as every other prompt/log/
            spinner in this codebase; ``render()`` is the only path to
            STDOUT).
        logger: Where a recovered ``AwsAdminCliError`` is debug-logged (in
            addition to the friendly message ``err_console`` gets) --
            unaffected by an unrecovered/unexpected exception, which is
            never caught here and so is never logged here either.
        prompter: Used ONLY for the dedicated ``EndpointUnavailableError``
            recovery ceremony (its blocking "press Enter" pause) -- every
            other prompt a flow needs comes from the flow's own ``Prompter``,
            never from here.

    Returns from ``run()``:
        0: navigation ended normally (EXIT, or BACK off the root).
        130: Ctrl+C at the root (matches ``main.py``'s own ``sys.exit(130)``
            for the exact same signal in non-interactive mode).
    """

    root: Navigable
    err_console: Console
    logger: logging.Logger
    prompter: Prompter
    _stack: list[Navigable] = field(init=False, repr=False)

    def __post_init__(self: Self) -> None:
        """Seed the stack with ``root``."""
        self._stack = [self.root]

    @property
    def depth(self: Self) -> int:
        """How many flows are currently on the stack (>= 1 while navigating)."""
        return len(self._stack)

    def push(self: Self, flow: Navigable) -> None:
        """Descend into ``flow``: it becomes the new top of the stack."""
        self._stack.append(flow)

    def run(self: Self) -> int:
        """Loop until the stack empties (or Ctrl+C at the root); return an exit code.

        An ``AwsAdminCliError`` raised by ``.menu()`` is recovered in place:
        cleared, printed, paused on an explicit ENTER, then the SAME flow is
        shown again -- unless ``AWS_ADMIN_CLI_DEBUG_TRACEBACK=1``, in which
        case it re-raises, mirroring ``main.py``'s own ``run()`` wrapper
        exactly, so a developer debugging a TUI flow sees the same behavior
        they already know from the non-interactive CLI. The pause matters:
        several flows (e.g. ``IamFlow``) make a live AWS call as the very
        first statement of ``.menu()``, before any prompt is shown, so a
        *persistent* failure (AccessDenied, throttling -- anything that
        doesn't self-heal) would otherwise retry that same failing call on
        every loop iteration with no human step in between: a silent,
        unbounded spin rather than a visible error the user can read and
        back out of with Ctrl+C. Any other exception is NOT caught here: it
        propagates out of ``run()`` and terminates the TUI (and, same as an
        uncaught exception anywhere else in this codebase, ultimately the
        process) -- an unexpected bug should always be loud.
        """
        debug_traceback = os.environ.get("AWS_ADMIN_CLI_DEBUG_TRACEBACK") == "1"
        # Wipe the screen before redrawing a menu, so backing out of ten submenus
        # doesn't leave ten menus of scrollback behind. Only when attached to a real
        # terminal: under CliRunner or a pipe there is nothing to clear and the
        # escape codes would just pollute captured output.
        clear_between_menus = self.err_console.is_terminal
        while self._stack:
            if clear_between_menus:
                clear_terminal()
            current = self._stack[-1]
            try:
                action = current.menu()
            except KeyboardInterrupt:
                if len(self._stack) == 1:
                    return 130
                self._stack.pop()
                continue
            except AwsAdminCliError as exc:
                if debug_traceback:
                    raise
                if isinstance(exc, EndpointUnavailableError):
                    self._handle_endpoint_unavailable(exc)
                else:
                    self._handle_recovered_error(exc)
                continue

            if action is NavAction.STAY:
                continue
            if action is NavAction.BACK:
                self._stack.pop()
                continue
            self._stack.clear()  # NavAction.EXIT
        return 0

    def _print_error(self: Self, exc: AwsAdminCliError) -> None:
        self.logger.debug("Recovered in TUI navigation: %s", exc)
        self.err_console.print(f"[red]Error:[/] {exc}")
        if exc.hint:
            self.err_console.print(f"[yellow]{exc.hint}[/]")

    def _handle_recovered_error(self: Self, exc: AwsAdminCliError) -> None:
        """Isolate, report, and pause on any other recovered ``AwsAdminCliError``.

        Same ceremony as ``_handle_endpoint_unavailable`` (clear, print,
        block on an explicit acknowledgement, clear again), generalized to
        every domain error a flow's ``.menu()`` can raise -- not just
        connectivity failures. Without this pause, a flow that makes a live
        AWS call as the first statement of ``.menu()`` (e.g. ``IamFlow``
        computing its header counts) turns a persistent failure into an
        unbounded retry loop: the exception fires again on the very next
        iteration, before the user ever sees a menu to back out of.
        Requiring ENTER between attempts makes the retry deliberate and
        human-paced; Ctrl+C (handled above) remains the escape hatch.
        """
        clear = self.err_console.is_terminal
        if clear:
            clear_terminal()
        self._print_error(exc)
        self.prompter.pause("Press ENTER to retry...")
        if clear:
            clear_terminal()

    def _handle_endpoint_unavailable(self: Self, exc: EndpointUnavailableError) -> None:
        """Isolate, report, and pause on a local-endpoint (e.g. LocalStack) connection failure.

        Every service (IAM, EC2, S3, VPC, Stack) reaches this same recovery
        path -- it never varies per flow, because every gateway call is
        already wrapped by ``infrastructure.aws.error_mapper``, and every
        flow's ``.menu()`` runs inside this same ``run()`` loop. Clears
        whatever partial menu/prompt was on screen, prints the fixed
        technical message, blocks on an explicit acknowledgement, then clears
        again so the current flow's menu redraws on a clean screen.
        """
        self.logger.debug("Recovered in TUI navigation (local endpoint down): %s", exc)
        clear = self.err_console.is_terminal
        if clear:
            clear_terminal()
        self.err_console.print(
            "[red][!] EndpointConnectionError: Connection refused. Could not establish "
            f"communication with {exc.endpoint_url}. Verify that the Docker daemon and "
            "the LocalStack container are running.[/]"
        )
        self.prompter.pause("Press ENTER to return to the menu...")
        if clear:
            clear_terminal()

"""Tests for ``NavigationStack``: BACK/STAY/EXIT semantics, error containment,
unexpected-exception termination, and bounded stack growth.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import pytest
from aws_admin_cli.core.exceptions import EndpointUnavailableError, ValidationError
from aws_admin_cli.presentation.tui.navigation import NavAction, NavigationStack
from rich.console import Console

from tests.fakes.prompter import FakePrompter


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=200)


def _logger() -> logging.Logger:
    return logging.getLogger("test.tui.navigation")


@dataclass
class _ScriptedFlow:
    """A ``Navigable`` whose ``.menu()`` pops one scripted step per call.

    Each step is either a ``NavAction`` to return, or an ``Exception``
    instance to raise. ``calls`` records how many times ``.menu()`` ran, so
    tests can assert recovery actually re-displayed the same flow rather
    than silently terminating.
    """

    title: str
    steps: list[NavAction | BaseException]
    calls: int = field(default=0)

    def menu(self) -> NavAction:
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def test_stay_redisplays_same_flow_then_back_ends_navigation() -> None:
    flow = _ScriptedFlow(title="root", steps=[NavAction.STAY, NavAction.STAY, NavAction.BACK])
    stack = NavigationStack(
        root=flow, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )

    exit_code = stack.run()

    assert exit_code == 0
    assert flow.calls == 3
    assert stack.depth == 0


def test_back_at_root_is_equivalent_to_exit() -> None:
    flow = _ScriptedFlow(title="root", steps=[NavAction.BACK])
    stack = NavigationStack(
        root=flow, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )

    assert stack.run() == 0
    assert stack.depth == 0


def test_exit_clears_the_whole_stack_from_any_depth() -> None:
    root = _ScriptedFlow(title="root", steps=[NavAction.STAY])
    child = _ScriptedFlow(title="child", steps=[NavAction.EXIT])
    stack = NavigationStack(
        root=root, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )
    stack.push(child)

    assert stack.depth == 2
    assert stack.run() == 0
    assert stack.depth == 0
    assert root.calls == 0  # never revisited: EXIT clears past the root immediately


def test_push_then_back_returns_control_to_the_parent() -> None:
    child = _ScriptedFlow(title="child", steps=[NavAction.BACK])
    pushed = False

    def root_menu() -> NavAction:
        nonlocal pushed
        if not pushed:
            pushed = True
            stack.push(child)
            return NavAction.STAY
        return NavAction.BACK

    root = _ScriptedFlow(title="root", steps=[])
    root.menu = root_menu  # type: ignore[method-assign]
    stack = NavigationStack(
        root=root, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )

    assert stack.run() == 0
    assert child.calls == 1
    assert stack.depth == 0


def test_keyboard_interrupt_at_root_exits_130() -> None:
    flow = _ScriptedFlow(title="root", steps=[KeyboardInterrupt()])
    stack = NavigationStack(
        root=flow, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )

    assert stack.run() == 130


def test_keyboard_interrupt_in_submenu_goes_back_instead_of_exiting() -> None:
    root = _ScriptedFlow(title="root", steps=[NavAction.BACK])
    child = _ScriptedFlow(title="child", steps=[KeyboardInterrupt()])
    stack = NavigationStack(
        root=root, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )
    stack.push(child)

    assert stack.run() == 0
    assert stack.depth == 0
    assert root.calls == 1  # control returned to root after the interrupt, then BACK


def test_aws_admin_cli_error_is_recovered_without_terminating() -> None:
    err_console = _console()
    flow = _ScriptedFlow(
        title="root",
        steps=[ValidationError("dato inválido", hint="corrige el dato"), NavAction.BACK],
    )
    stack = NavigationStack(
        root=flow, err_console=err_console, logger=_logger(), prompter=FakePrompter()
    )

    exit_code = stack.run()

    assert exit_code == 0
    assert flow.calls == 2  # menu() was called again after the recovered error
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "dato inválido" in output
    assert "corrige el dato" in output


def test_aws_admin_cli_error_reraises_when_debug_traceback_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ADMIN_CLI_DEBUG_TRACEBACK", "1")
    flow = _ScriptedFlow(title="root", steps=[ValidationError("dato inválido")])
    stack = NavigationStack(
        root=flow, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )

    with pytest.raises(ValidationError):
        stack.run()


def test_endpoint_unavailable_prints_fixed_message_pauses_and_returns_to_same_flow() -> None:
    err_console = _console()
    prompter = FakePrompter()
    flow = _ScriptedFlow(
        title="root",
        steps=[
            EndpointUnavailableError(
                "No se pudo contactar a s3 (ListBuckets)",
                endpoint_url="http://localhost:4566",
                hint="¿Está LocalStack corriendo? Ejecuta `make up`.",
            ),
            NavAction.BACK,
        ],
    )
    stack = NavigationStack(root=flow, err_console=err_console, logger=_logger(), prompter=prompter)

    exit_code = stack.run()

    assert exit_code == 0
    assert flow.calls == 2  # menu() was called again after the recovered error
    output = err_console.file.getvalue()  # type: ignore[attr-defined]
    assert (
        "[!] EndpointConnectionError: Connection refused. Could not establish communication "
        "with http://localhost:4566. Verify that the Docker daemon and the LocalStack "
        "container are running." in output
    )


def test_endpoint_unavailable_reraises_when_debug_traceback_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ADMIN_CLI_DEBUG_TRACEBACK", "1")
    flow = _ScriptedFlow(
        title="root",
        steps=[EndpointUnavailableError("boom", endpoint_url="http://localhost:4566")],
    )
    stack = NavigationStack(
        root=flow, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )

    with pytest.raises(EndpointUnavailableError):
        stack.run()


def test_unexpected_exception_is_not_recovered_and_terminates() -> None:
    flow = _ScriptedFlow(title="root", steps=[RuntimeError("bug inesperado")])
    stack = NavigationStack(
        root=flow, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )

    with pytest.raises(RuntimeError, match="bug inesperado"):
        stack.run()


def test_no_unbounded_stack_growth_over_50_navigations() -> None:
    """Push-then-immediately-BACK, 50 times: depth must never exceed 2."""
    max_depth_seen = 0
    remaining = 50

    def child_menu() -> NavAction:
        return NavAction.BACK

    def make_child() -> _ScriptedFlow:
        child = _ScriptedFlow(title="child", steps=[])
        child.menu = child_menu  # type: ignore[method-assign]
        return child

    def root_menu() -> NavAction:
        nonlocal remaining, max_depth_seen
        if remaining <= 0:
            return NavAction.BACK
        remaining -= 1
        stack.push(make_child())
        max_depth_seen = max(max_depth_seen, stack.depth)
        return NavAction.STAY

    root = _ScriptedFlow(title="root", steps=[])
    root.menu = root_menu  # type: ignore[method-assign]
    stack = NavigationStack(
        root=root, err_console=_console(), logger=_logger(), prompter=FakePrompter()
    )

    assert stack.run() == 0
    assert remaining == 0
    assert max_depth_seen <= 2
    assert stack.depth == 0

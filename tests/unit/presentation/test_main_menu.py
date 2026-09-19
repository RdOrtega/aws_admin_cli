"""Tests for the TUI's root menu (``_MainMenu``): banner, choices, wiring, and the
exit farewell -- see ``presentation/tui/app.py``.
"""

import io
import socket
from contextlib import closing
from dataclasses import replace

import pytest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.app import (
    _FAREWELL_MESSAGE,
    _MainMenu,
    _print_farewell,
    run_interactive,
)
from aws_admin_cli.presentation.tui.menu import NAV_EXIT, Choice
from aws_admin_cli.presentation.tui.navigation import NavAction
from rich.console import Console

from tests.fakes.prompter import FakePrompter

_APP_MODULE = "aws_admin_cli.presentation.tui.app"
# Never actually listened on by this test suite -- refuses instantly on localhost, so
# tests that don't care about connectivity get a deterministic OFFLINE.
_CLOSED_LOCAL_ENDPOINT = "http://127.0.0.1:1"


def _console(*, force_terminal: bool = False) -> Console:
    return Console(file=io.StringIO(), force_terminal=force_terminal, width=200)


def _ctx(settings: Settings, *, force_terminal: bool = False) -> AppContext:
    base = AppContext.build(settings)
    return replace(base, err_console=_console(force_terminal=force_terminal))


def _bind_free_port() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, server.getsockname()[1]


class _StackSpy:
    """Records ``push()`` calls -- stands in for ``NavigationStack`` in isolated tests."""

    def __init__(self) -> None:
        self.pushed: list[object] = []

    def push(self, flow: object) -> None:
        self.pushed.append(flow)


# -- Choices ------------------------------------------------------------------------


def test_choices_are_exactly_the_seven_modules_in_order_then_exit() -> None:
    ctx = _ctx(Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT))
    choices = _MainMenu(ctx=ctx, prompter=FakePrompter())._choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "🌍 Environment & Region Context",
        "🔐 IAM Access & Identity Management",
        "🖥️ EC2 Compute & Fleet Control",
        "⚡ AWS Lambda & Serverless",
        "📦 S3 Storage & Security Governance",
        "📈 CloudWatch Observability & Logs",
        "🛡️ Security & Compliance Audit",
        "─" * 70,
        "🚪 Exit CLI Session",
    ]


def test_selecting_a_flow_pushes_it_onto_the_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic routing test -- not about connectivity, so the circuit breaker
    (see the "Circuit breaker" tests below) is stubbed healthy here.
    """
    monkeypatch.setattr(f"{_APP_MODULE}.aws_connection_is_healthy", lambda ctx: True)
    ctx = _ctx(Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT))
    prompter = FakePrompter(["S3Flow"])
    menu = _MainMenu(ctx=ctx, prompter=prompter)
    spy = _StackSpy()
    menu.stack = spy  # type: ignore[assignment]

    action = menu.menu()

    assert action is NavAction.STAY
    assert [type(f).__name__ for f in spy.pushed] == ["S3Flow"]


def test_selecting_exit_returns_exit_action() -> None:
    ctx = _ctx(Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT))
    menu = _MainMenu(ctx=ctx, prompter=FakePrompter([NAV_EXIT]))

    assert menu.menu() is NavAction.EXIT


# -- Status color/text logic (pure -- no Rich rendering involved) -------------------


def test_resolve_status_is_online_green_when_the_local_endpoint_is_reachable() -> None:
    server, port = _bind_free_port()
    with closing(server):
        status = _MainMenu._resolve_status(f"http://127.0.0.1:{port}")
    assert status == ("ONLINE", "green")


def test_resolve_status_is_offline_red_when_the_local_endpoint_refuses_connection() -> None:
    assert _MainMenu._resolve_status(_CLOSED_LOCAL_ENDPOINT) == ("OFFLINE", "red")


def test_resolve_status_is_na_dim_when_targeting_real_aws() -> None:
    assert _MainMenu._resolve_status(None) == ("N/A", "dim")


def test_menu_reflects_the_endpoint_going_down_between_two_redraws() -> None:
    """Two consecutive ``menu()`` calls -- exactly what ``NavigationStack.run()`` does
    every time control returns to the root -- must reflect the live state at each
    call, not a stale first reading: no caching anywhere in the banner's path.
    """
    server, port = _bind_free_port()
    ctx = _ctx(Settings(profile="localstack", endpoint_url=f"http://127.0.0.1:{port}"))
    menu = _MainMenu(ctx=ctx, prompter=FakePrompter([NAV_EXIT, NAV_EXIT]))

    menu.menu()  # first redraw: container running
    first_output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]

    server.close()  # container stopped mid-session, same address

    menu.menu()  # second redraw: same endpoint, now down
    second_output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]

    assert "ONLINE" in first_output
    assert "OFFLINE" in second_output


# -- Banner text content --------------------------------------------------------------


def test_banner_prints_title_target_region_and_user_for_a_local_target() -> None:
    """Offline here, so the Region field shows the unreachable warning, not the
    region name -- see the dedicated offline/online region tests below.
    """
    ctx = _ctx(
        Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT, region="eu-west-1")
    )
    _MainMenu(ctx=ctx, prompter=FakePrompter())._render_banner()

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "AWS CLOUD ADMIN CLI v1.0" in output
    assert "LOCALSTACK (Local)" in output
    assert "admin" in output
    assert "OFFLINE" in output


def test_banner_shows_the_unreachable_warning_instead_of_the_region_when_offline() -> None:
    ctx = _ctx(
        Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT, region="eu-west-1")
    )
    _MainMenu(ctx=ctx, prompter=FakePrompter())._render_banner()

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Region: ⚠️  (Unreachable)" in output
    assert "eu-west-1" not in output


def test_banner_shows_the_region_when_online() -> None:
    server, port = _bind_free_port()
    with closing(server):
        ctx = _ctx(
            Settings(
                profile="localstack",
                endpoint_url=f"http://127.0.0.1:{port}",
                region="eu-west-1",
            )
        )
        _MainMenu(ctx=ctx, prompter=FakePrompter())._render_banner()

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "ONLINE" in output
    assert "eu-west-1" in output
    assert "(Unreachable)" not in output


def test_banner_shows_real_aws_target_na_user_for_a_real_aws_target() -> None:
    ctx = _ctx(Settings(profile="testprofile"))
    _MainMenu(ctx=ctx, prompter=FakePrompter())._render_banner()

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "AWS CLOUD (Live)" in output
    assert "N/A" in output


# -- Exit / farewell -----------------------------------------------------------------


def test_print_farewell_prints_the_message() -> None:
    ctx = _ctx(Settings(profile="localstack"))

    _print_farewell(ctx)

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert _FAREWELL_MESSAGE in output


def test_print_farewell_clears_only_when_attached_to_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(f"{_APP_MODULE}.clear_terminal", lambda: calls.append(True))

    _print_farewell(_ctx(Settings(profile="localstack"), force_terminal=False))
    assert calls == []

    _print_farewell(_ctx(Settings(profile="localstack"), force_terminal=True))
    assert calls == [True]


def test_run_interactive_selecting_exit_returns_zero_and_prints_the_farewell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT))
    fake_prompter = FakePrompter([NAV_EXIT])
    monkeypatch.setattr(f"{_APP_MODULE}.QuestionaryPrompter", lambda: fake_prompter)

    exit_code = run_interactive(ctx)

    assert exit_code == 0
    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert _FAREWELL_MESSAGE in output


# -- Circuit breaker: block entry into a connectivity-gated module when offline -----


@pytest.mark.parametrize(
    "flow_key", ["IamFlow", "Ec2Flow", "LambdaFlow", "S3Flow", "CloudWatchFlow", "AuditFlow"]
)
def test_circuit_breaker_blocks_a_gated_flow_when_the_connection_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch, flow_key: str
) -> None:
    monkeypatch.setattr(f"{_APP_MODULE}.aws_connection_is_healthy", lambda ctx: False)
    ctx = _ctx(Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT))
    prompter = FakePrompter([flow_key])
    menu = _MainMenu(ctx=ctx, prompter=prompter)
    spy = _StackSpy()
    menu.stack = spy  # type: ignore[assignment]

    action = menu.menu()

    assert action is NavAction.STAY
    assert spy.pushed == []


@pytest.mark.parametrize(
    "flow_key", ["IamFlow", "Ec2Flow", "LambdaFlow", "S3Flow", "CloudWatchFlow", "AuditFlow"]
)
def test_circuit_breaker_allows_a_gated_flow_when_the_connection_is_healthy(
    monkeypatch: pytest.MonkeyPatch, flow_key: str
) -> None:
    monkeypatch.setattr(f"{_APP_MODULE}.aws_connection_is_healthy", lambda ctx: True)
    ctx = _ctx(Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT))
    prompter = FakePrompter([flow_key])
    menu = _MainMenu(ctx=ctx, prompter=prompter)
    spy = _StackSpy()
    menu.stack = spy  # type: ignore[assignment]

    action = menu.menu()

    assert action is NavAction.STAY
    assert [type(f).__name__ for f in spy.pushed] == [flow_key]


def test_circuit_breaker_never_gates_environment_flow() -> None:
    """``EnvironmentFlow`` makes no AWS calls and is where an admin would go to
    check status or switch region -- it must stay reachable even offline, so it
    is never run through ``aws_connection_is_healthy`` at all.
    """
    ctx = _ctx(Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT))
    prompter = FakePrompter(["EnvironmentFlow"])
    menu = _MainMenu(ctx=ctx, prompter=prompter)
    spy = _StackSpy()
    menu.stack = spy  # type: ignore[assignment]

    action = menu.menu()

    assert action is NavAction.STAY
    assert [type(f).__name__ for f in spy.pushed] == ["EnvironmentFlow"]


def test_circuit_breaker_prints_the_structured_diagnostic_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_APP_MODULE}.aws_connection_is_healthy", lambda ctx: False)
    ctx = _ctx(Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT))
    prompter = FakePrompter(["Ec2Flow"])
    menu = _MainMenu(ctx=ctx, prompter=prompter)
    menu.stack = _StackSpy()  # type: ignore[assignment]

    menu.menu()

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "[!] Connection Error: Unable to reach AWS/LocalStack target endpoint." in output
    assert (
        "[!] Diagnostic: Docker container is inactive or network connection timed out."
        in output
    )
    assert (
        "[→] Remediation: Ensure LocalStack is running ('docker start localstack') "
        "or verify credentials." in output
    )


def test_circuit_breaker_does_not_crash_or_leak_a_traceback_when_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the gate: entering a module while offline is a normal,
    recoverable outcome -- STAY at the main menu, nothing raised.
    """
    monkeypatch.setattr(f"{_APP_MODULE}.aws_connection_is_healthy", lambda ctx: False)
    ctx = _ctx(Settings(profile="localstack", endpoint_url=_CLOSED_LOCAL_ENDPOINT))
    prompter = FakePrompter(["IamFlow"])
    menu = _MainMenu(ctx=ctx, prompter=prompter)
    menu.stack = _StackSpy()  # type: ignore[assignment]

    action = menu.menu()  # must not raise

    assert action is NavAction.STAY

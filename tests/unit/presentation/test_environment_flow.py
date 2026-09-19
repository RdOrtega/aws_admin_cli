"""Tests for ``EnvironmentFlow``: the interactive Region Selector."""

import io

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.flows.environment_flow import EnvironmentFlow
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, plain_text
from aws_admin_cli.presentation.tui.navigation import NavAction
from rich.console import Console

from tests.fakes.prompter import FakePrompter


def _ctx(settings: Settings) -> AppContext:
    from dataclasses import replace

    base = AppContext.build(settings)
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200)
    return replace(base, console=console, err_console=err_console)


class _SpyPrompter(FakePrompter):
    """Records the ``choices`` list a ``select()`` call was offered, not just the message."""

    def __init__(self, responses: list[object]) -> None:
        super().__init__(responses)
        self.offered_choices: list[object] | None = None

    def select(  # type: ignore[override]
        self, message, choices, *, default=None, use_search=False, spacing=True
    ):
        self.offered_choices = list(choices)
        return super().select(
            message, choices, default=default, use_search=use_search, spacing=spacing
        )


def test_menu_lists_every_region_with_simulated_health_then_backs_out() -> None:
    ctx = _ctx(Settings(profile="localstack", region="eu-west-1"))
    prompter = _SpyPrompter([NAV_BACK])

    action = EnvironmentFlow(ctx, prompter).menu()

    assert action is NavAction.BACK
    assert prompter.offered_choices is not None
    labels = [plain_text(c.title) for c in prompter.offered_choices if hasattr(c, "title")]
    assert any("us-east-1" in label and "ONLINE" in label for label in labels)
    assert any("eu-west-1" in label and "ACTIVE" in label for label in labels)  # the active region
    assert any("ap-northeast-1" in label and "UNREACHABLE" in label for label in labels)
    assert any(label == "↩️  Back" for label in labels)


def test_menu_labels_show_the_friendly_name_for_every_region() -> None:
    ctx = _ctx(Settings(profile="localstack", region="us-east-1"))
    prompter = _SpyPrompter([NAV_BACK])

    EnvironmentFlow(ctx, prompter).menu()

    assert prompter.offered_choices is not None
    labels = [plain_text(c.title) for c in prompter.offered_choices if hasattr(c, "title")]
    assert any("us-east-1 (N. Virginia)" in label for label in labels)
    assert any("us-west-2 (Oregon)" in label for label in labels)
    assert any("eu-west-1 (Ireland)" in label for label in labels)
    assert any("sa-east-1 (São Paulo)" in label for label in labels)
    assert any("ap-northeast-1 (Tokyo)" in label for label in labels)


def test_menu_shows_the_active_badge_only_for_the_currently_active_region() -> None:
    ctx = _ctx(Settings(profile="localstack", region="eu-west-1"))
    prompter = _SpyPrompter([NAV_BACK])

    EnvironmentFlow(ctx, prompter).menu()

    assert prompter.offered_choices is not None
    labels = {
        c.value: plain_text(c.title) for c in prompter.offered_choices if hasattr(c, "title")
    }
    assert "[✔ ACTIVE]" in labels["eu-west-1"]
    assert "[● ONLINE]" not in labels["eu-west-1"]  # replaced, not appended alongside
    for code, label in labels.items():
        if code != "eu-west-1":
            assert "[✔ ACTIVE]" not in label


def test_active_regions_badge_is_a_native_formatted_title_not_raw_markup() -> None:
    """The ACTIVE badge must color via a ``StyledTitle`` fragment, never Rich markup or raw
    ANSI baked into a plain string -- either would show as literal bracket text (or corrupt
    column alignment) through questionary/prompt_toolkit. See ``menu.StyledTitle``."""
    ctx = _ctx(Settings(profile="localstack", region="us-west-2"))
    prompter = _SpyPrompter([NAV_BACK])

    EnvironmentFlow(ctx, prompter).menu()

    assert prompter.offered_choices is not None
    active_choice = next(
        c for c in prompter.offered_choices if getattr(c, "value", None) == "us-west-2"
    )
    assert isinstance(active_choice.title, list)
    styles = [style for style, _text in active_choice.title]
    assert any("green" in style for style in styles)
    flattened = plain_text(active_choice.title)
    assert "[bold green]" not in flattened
    assert "[/]" not in flattened


def test_selecting_exit_returns_exit_action() -> None:
    ctx = _ctx(Settings(profile="localstack"))
    prompter = FakePrompter([NAV_EXIT])

    assert EnvironmentFlow(ctx, prompter).menu() is NavAction.EXIT


def test_title_matches_the_main_menu_label() -> None:
    assert EnvironmentFlow.title == "🌍 Environment & Region Context"


# -- Region switching ----------------------------------------------------------------


def test_selecting_an_online_region_updates_settings_region_globally() -> None:
    ctx = _ctx(Settings(profile="localstack", region="us-east-1"))
    prompter = FakePrompter(["eu-west-1"])

    action = EnvironmentFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert ctx.settings.region == "eu-west-1"


def test_selecting_an_online_region_shows_the_success_banner() -> None:
    ctx = _ctx(Settings(profile="localstack", region="us-east-1"))
    prompter = FakePrompter(["sa-east-1"])

    EnvironmentFlow(ctx, prompter).menu()

    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Global region context updated to 'sa-east-1'." in output


def test_selecting_an_online_region_refreshes_the_header_with_the_new_region() -> None:
    ctx = _ctx(Settings(profile="localstack", region="us-east-1"))
    prompter = FakePrompter(["us-west-2"])

    EnvironmentFlow(ctx, prompter).menu()

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "us-west-2 (Oregon)" in output


def test_selecting_an_online_region_clears_the_client_cache() -> None:
    """A client built under the old region must not be handed back stale after a switch."""
    ctx = _ctx(Settings(profile="localstack", region="us-east-1"))
    ctx.client_factory._client_cache["s3"] = object()
    prompter = FakePrompter(["eu-west-1"])

    EnvironmentFlow(ctx, prompter).menu()

    assert ctx.client_factory._client_cache == {}


def test_selecting_the_degraded_region_blocks_and_shows_error_banner() -> None:
    ctx = _ctx(Settings(profile="localstack", region="us-east-1"))
    prompter = FakePrompter(["ap-northeast-1"])

    action = EnvironmentFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert ctx.settings.region == "us-east-1"  # unchanged
    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Region 'ap-northeast-1' is currently degraded. Selection aborted." in output

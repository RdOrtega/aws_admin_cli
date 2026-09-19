"""The "Environment & Region Context" TUI screen: the interactive Region Selector.

Replaces the old read-only settings-table snapshot with the session's global
region switch: picking an ONLINE region here updates ``ctx.settings.region``
for every flow and every boto3 client this session builds from then on (see
``AppContext.set_region``), not just this screen's own display. The listed
health status per region is simulated (a fixed, hardcoded table), not a live
health check -- there is no AWS call this screen could make that would tell
it whether a *region*, as opposed to a specific service endpoint, is healthy.
"""

from dataclasses import dataclass
from typing import ClassVar, Final, Self

from aws_admin_cli.core.context import AppContext
from aws_admin_cli.domain.constants import get_region_display_name
from aws_admin_cli.presentation.tui.flows._shared import announce_result, clear_and_banner
from aws_admin_cli.presentation.tui.menu import (
    NAV_BACK,
    NAV_EXIT,
    Choice,
    Separator,
    StyledTitle,
)
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter

__all__ = ["EnvironmentFlow"]

_ACTIVE_STATUS = "[✔ ACTIVE]"
_ONLINE_STATUS = "[● ONLINE]"
_DEGRADED_STATUS = "[🔴 UNREACHABLE]"

# ``fg:ansigreen bold`` is a prompt_toolkit style string, applied only to the
# ACTIVE badge fragment -- see ``StyledTitle``'s docstring (menu.py) for why
# this fragment form, and never a Rich-markup or raw-ANSI-escaped *string*,
# is what actually colors correctly inside a questionary choice.
_ACTIVE_STYLE = "fg:ansigreen bold"

# (region code, online?) -- simulated health, not a live check (see the module
# docstring). Friendly names come from the shared ``AWS_REGION_NAMES`` mapping
# (``domain.constants``), not duplicated here. Adding a region later is adding
# one row here (plus its friendly name there); nothing else in this module
# branches on a specific region code.
_REGIONS: Final[tuple[tuple[str, bool], ...]] = (
    ("us-east-1", True),
    ("us-west-2", True),
    ("eu-west-1", True),
    ("sa-east-1", True),
    ("ap-northeast-1", False),
)

_DEGRADED_REGIONS: Final[frozenset[str]] = frozenset(
    code for code, online in _REGIONS if not online
)


def _region_choices(active_region: str) -> list[Choice | Separator]:
    """Build the region picker: one row per ``_REGIONS`` entry, aligned on the status column.

    The region matching ``active_region`` (the session's current global
    region, ``ctx.settings.region``) shows a colored ``[✔ ACTIVE]`` badge in
    place of its ``[● ONLINE]`` one -- the picker's own current selection is
    then visually obvious, not just inferable from list order. The label
    portion is always padded to the same fixed width first, so this
    badge -- like the plain-text ``[● ONLINE]``/``[🔴 UNREACHABLE]`` ones --
    starts in the same column on every row regardless of which one it is.
    """

    def _label(code: str) -> str:
        return f"🌍 {get_region_display_name(code)}"

    labels = [_label(code) for code, _online in _REGIONS]
    width = max(len(label) for label in labels) + 2

    choices: list[Choice | Separator] = []
    for (code, online), label in zip(_REGIONS, labels, strict=True):
        padded_label = f"{label:<{width}}"
        title: str | StyledTitle
        if code == active_region:
            title = [("", padded_label), (_ACTIVE_STYLE, _ACTIVE_STATUS)]
        else:
            title = f"{padded_label}{_ONLINE_STATUS if online else _DEGRADED_STATUS}"
        choices.append(Choice(title=title, value=code))
    choices.append(Separator())
    choices.append(Choice(title="↩️  Back", value=NAV_BACK))
    return choices


@dataclass(slots=True)
class EnvironmentFlow:
    """Interactive Region Selector: this session's global region switch."""

    title: ClassVar[str] = "🌍 Environment & Region Context"

    ctx: AppContext
    prompter: Prompter

    def menu(self: Self) -> NavAction:
        """Show the region picker once; selecting an ONLINE region switches globally."""
        clear_and_banner(self.ctx)
        choices = _region_choices(self.ctx.settings.region)
        selected = self.prompter.select("Select a region:", choices)
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected == NAV_BACK:
            return NavAction.BACK
        self._select_region(selected)
        return NavAction.STAY

    def _select_region(self: Self, region: str) -> None:
        if region in _DEGRADED_REGIONS:
            self.ctx.err_console.print(
                f"[bold red]Region '{region}' is currently degraded. Selection aborted.[/]"
            )
            self.prompter.pause()
            return
        self.ctx.set_region(region)
        clear_and_banner(self.ctx)  # redraw the header now, so it shows the new region at once
        announce_result(
            self.ctx,
            self.prompter,
            f"[bold green]Global region context updated to '{region}'.[/]",
        )

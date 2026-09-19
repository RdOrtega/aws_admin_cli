"""Menu vocabulary shared by every TUI flow: choices, separators, and navigation sentinels.

Deliberately independent of ``questionary``: a ``Flow`` builds a plain list of
``Choice``/``Separator`` and hands it to a ``Prompter`` (a port) -- the only
place that ever imports ``questionary`` is ``prompter.py``'s
``QuestionaryPrompter`` adapter, which translates these into the library's
own ``Choice``/``Separator`` types.
"""

from dataclasses import dataclass
from typing import Final

__all__ = [
    "NAV_BACK",
    "NAV_EXIT",
    "Choice",
    "StyledTitle",
    "Separator",
    "back_and_exit_choices",
    "plain_text",
]

# A prompt_toolkit/questionary-compatible "formatted text" title: a sequence of
# (style, text) fragments rendered with per-fragment styling -- e.g.
# ``[("", "us-west-2 "), ("fg:ansigreen bold", "[ACTIVE]")]``. A plain ``str``
# title never needs this; reach for it only when one *part* of a choice's
# label needs its own color. NEVER embed Rich markup (``[bold green]...[/]``)
# or raw ANSI escape codes in a plain ``str`` title instead -- questionary
# shows Rich markup as literal bracket text (it doesn't parse Rich's bbcode),
# and prompt_toolkit measures a plain string's on-screen width by counting
# its raw characters (escape bytes included), so raw ANSI silently corrupts
# the menu's column alignment and selection highlight. This fragment form is
# the only one prompt_toolkit measures and colors correctly.
StyledTitle = list[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Choice:
    """One selectable menu entry.

    Attributes:
        title: What the user sees -- a plain string, or (when part of the
            label needs its own color) a ``StyledTitle`` fragment list. See
            ``StyledTitle``'s own docstring for why raw ANSI/Rich markup
            strings are never the right way to color part of a title.
        value: What ``Prompter.select``/``checkbox`` returns when this entry
            is chosen -- a flow switches on this, never on ``title``.
        disabled: If set, the reason this entry can't be picked right now
            (shown grayed-out with this text); ``None`` means selectable.
        checked: ``checkbox`` only -- whether this entry starts pre-ticked.
            Lets a flow re-show a checkbox with a previous selection intact
            (e.g. after the user picks "<- Back" from a follow-up confirm
            and returns to the same checkbox) instead of losing it. Ignored
            by ``select``.
    """

    title: str | StyledTitle
    value: str
    disabled: str | None = None
    checked: bool = False


def plain_text(title: str | StyledTitle) -> str:
    """The unstyled text of a ``Choice.title`` -- a ``StyledTitle`` flattened, or ``title`` as-is.

    For code (tests, logs) that only cares what a choice *says*, not how a
    ``StyledTitle`` fragment list colors it.
    """
    if isinstance(title, str):
        return title
    return "".join(text for _style, text in title)


@dataclass(frozen=True, slots=True)
class Separator:
    """A non-selectable visual divider in a menu list.

    One width for every menu in the TUI: the divider is part of the shared
    screen template, so a service screen and a detail screen must not draw
    it differently.
    """

    line: str = "\u2500" * 66


NAV_BACK: Final[str] = "__nav:back__"
NAV_EXIT: Final[str] = "__nav:exit__"


def back_and_exit_choices() -> list[Choice | Separator]:
    """The trailing ``[Separator, "↩️  Back", "Exit"]`` every submenu's choice list ends with."""
    return [
        Separator(),
        Choice(title="↩️  Back", value=NAV_BACK),
        Choice(title="Exit", value=NAV_EXIT),
    ]

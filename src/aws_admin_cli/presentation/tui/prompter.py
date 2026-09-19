"""The ``Prompter`` port: every user-facing prompt a TUI flow can ask.

One cancellation contract, honored by every method: the instant the user
cancels (Ctrl+C, Esc), the method returns ``None`` (``False`` for
``confirm``) -- it NEVER lets ``KeyboardInterrupt`` (or ``EOFError``, from a
closed/non-interactive stdin) propagate out. A ``Flow`` never wraps a call to
a ``Prompter`` method in a ``try/except`` for either of those; it only ever
checks the return value. This is what lets ``NavigationStack`` treat "the
user backed out of a prompt" as ordinary control flow, not an exception path.

``QuestionaryPrompter`` is the only implementation that talks to a real
terminal (via ``questionary``); tests use ``tests/fakes/prompter.py``'s
``FakePrompter`` instead, never a ``Mock()``.

Global vertical spacing: ``select``/``checkbox`` (the actual multi-option
*menus*) each print one blank line before asking -- centralized here so
every flow gets breathing room around its menus without touching each flow
file. ``text``/``confirm``/``path``/``pause`` stay tight: they're single-line
prompts, often chained several in a row (e.g. a create form), where a blank
line before each would feel like noise rather than structure.
"""

from collections.abc import Callable, Sequence
from typing import Any, Protocol, Self

import questionary

from aws_admin_cli.presentation.tui.menu import Choice, Separator
from aws_admin_cli.presentation.tui.theme import TUI_STYLE

__all__ = ["Prompter", "QuestionaryPrompter"]

# questionary's own default checkbox instruction ("(Use arrow keys to move, <space> to
# select, <a> to toggle, <i> to invert)") renders on the SAME line as the question by
# default -- fine for a short question, but it collides horizontally with a longer one
# (e.g. "Which policies do you want to attach? (optional)"). The leading ``\n`` is not
# decorative: prompt_toolkit's formatted-text renderer breaks a line wherever a fragment
# contains one, so this is what actually pushes the instruction onto its own line, two
# spaces indented to sit under the question text, for every checkbox prompt in the TUI.
_CHECKBOX_INSTRUCTION = (
    "\n  (Use arrow keys to move, <space> to select, <a> to toggle, <i> to invert)"
)


class Prompter(Protocol):
    """Port: the prompt primitives every TUI flow is built from."""

    def select(
        self: Self,
        message: str,
        choices: Sequence[Choice | Separator],
        *,
        default: str | None = None,
        use_search: bool = False,
        spacing: bool = True,
    ) -> str | None:
        """Ask the user to pick one entry; returns its ``Choice.value``, ``None`` if cancelled.

        ``spacing`` controls the blank line normally printed above the menu.
        A caller that has just printed its own header (the service screens'
        stats line) passes ``False`` so the header sits directly on top of
        the question instead of being pushed away from it.

        ``use_search`` turns on a type-to-filter box -- flows set it for long
        dynamic lists (see the "dynamic list pattern": ``search=True`` above
        ~25 items) so scrolling through dozens of buckets/instances/roles
        isn't the only way to find one.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def checkbox(
        self: Self, message: str, choices: Sequence[Choice | Separator]
    ) -> list[str] | None:
        """Ask the user to pick zero or more entries; ``None`` (not ``[]``) if cancelled."""
        ...  # pragma: no cover -- Protocol body, never executed

    def text(
        self: Self,
        message: str,
        *,
        default: str = "",
        validate: Callable[[str], bool | str] | None = None,
    ) -> str | None:
        """Ask for free-text input. ``None`` if cancelled."""
        ...  # pragma: no cover -- Protocol body, never executed

    def confirm(self: Self, message: str, *, default: bool = False) -> bool:
        """Ask a yes/no question. ``False`` if cancelled -- same as answering "no"."""
        ...  # pragma: no cover -- Protocol body, never executed

    def path(self: Self, message: str, *, only_directories: bool = False) -> str | None:
        """Ask for a filesystem path, with path completion. ``None`` if cancelled."""
        ...  # pragma: no cover -- Protocol body, never executed

    def pause(self: Self, message: str = "Press Enter to continue...") -> None:
        """Block until Enter is pressed -- a pacing pause, not a question with an answer."""
        ...  # pragma: no cover -- Protocol body, never executed


def _ask(question: questionary.Question) -> Any:
    """Run a questionary ``Question``, folding a closed/non-interactive stdin into ``None`` too.

    ``Question.ask()`` already catches ``KeyboardInterrupt`` and returns
    ``None``; it does NOT catch ``EOFError`` (raised by prompt_toolkit if
    stdin hits EOF, e.g. a closed pipe) -- this is the one place that closes
    that gap, so every ``QuestionaryPrompter`` method inherits it for free.
    """
    try:
        return question.ask()
    except EOFError:
        return None


def _to_questionary_choices(choices: Sequence[Choice | Separator]) -> list[questionary.Choice]:
    result: list[questionary.Choice] = []
    for choice in choices:
        if isinstance(choice, Separator):
            result.append(questionary.Separator(choice.line))
        else:
            result.append(
                questionary.Choice(
                    title=choice.title,
                    value=choice.value,
                    disabled=choice.disabled,
                    checked=choice.checked,
                )
            )
    return result


class QuestionaryPrompter:
    """``Prompter`` backed by ``questionary`` -- arrow-key-navigable prompts on a real terminal."""

    def select(
        self: Self,
        message: str,
        choices: Sequence[Choice | Separator],
        *,
        default: str | None = None,
        use_search: bool = False,
        spacing: bool = True,
    ) -> str | None:
        """See ``Prompter.select``."""
        if spacing:
            print()  # global vertical spacing before every questionary menu
        result: str | None = _ask(
            questionary.select(
                message,
                choices=_to_questionary_choices(choices),
                default=default,
                use_search_filter=use_search,
                # questionary raises ValueError at construction time if both are True
                # ("Cannot use j/k keys with prefix filter search, since j/k can be part
                # of the prefix.") -- search-typed text must win over vi-style j/k nav.
                use_jk_keys=not use_search,
                style=TUI_STYLE,
            )
        )
        return result

    def checkbox(
        self: Self, message: str, choices: Sequence[Choice | Separator]
    ) -> list[str] | None:
        """See ``Prompter.checkbox``."""
        print()  # global vertical spacing before every questionary menu
        result: list[str] | None = _ask(
            questionary.checkbox(
                message,
                choices=_to_questionary_choices(choices),
                instruction=_CHECKBOX_INSTRUCTION,
                style=TUI_STYLE,
            )
        )
        return result

    def text(
        self: Self,
        message: str,
        *,
        default: str = "",
        validate: Callable[[str], bool | str] | None = None,
    ) -> str | None:
        """See ``Prompter.text``."""
        result: str | None = _ask(
            questionary.text(message, default=default, validate=validate, style=TUI_STYLE)
        )
        return result

    def confirm(self: Self, message: str, *, default: bool = False) -> bool:
        """See ``Prompter.confirm``."""
        result: bool | None = _ask(questionary.confirm(message, default=default, style=TUI_STYLE))
        return bool(result)

    def path(self: Self, message: str, *, only_directories: bool = False) -> str | None:
        """See ``Prompter.path``."""
        result: str | None = _ask(
            questionary.path(message, only_directories=only_directories, style=TUI_STYLE)
        )
        return result

    def pause(self: Self, message: str = "Press Enter to continue...") -> None:
        """See ``Prompter.pause``."""
        _ask(questionary.text(message, style=TUI_STYLE))

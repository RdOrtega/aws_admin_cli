"""In-memory ``Prompter`` test double: a scripted response queue, not a ``Mock()``.

A ``Mock()`` answers anything asked of it, which lets a flow drift out of
sync with a test (asking one prompt more, or a different one, than the test
expects) without either side noticing. ``FakePrompter`` refuses to: it
answers each prompt with the next item from a pre-scripted queue, records
every question it was asked (so a test can assert on the exact prompts a
flow issued, in order), and raises loudly the moment a flow asks for one
more answer than the test scripted.
"""

from collections import deque
from collections.abc import Callable, Sequence
from typing import Any

from aws_admin_cli.presentation.tui.menu import Choice, Separator


class FakePrompter:
    """A structurally-typed ``Prompter`` double, driven by a scripted response queue."""

    def __init__(self, responses: Sequence[Any] = ()) -> None:
        self.responses: deque[Any] = deque(responses)
        self.asked: list[str] = []

    def _next(self, message: str) -> Any:
        self.asked.append(message)
        if not self.responses:
            raise AssertionError(
                f"FakePrompter: se preguntó '{message}' pero no quedan respuestas guionizadas."
            )
        return self.responses.popleft()

    def select(
        self,
        message: str,
        choices: Sequence[Choice | Separator],
        *,
        default: str | None = None,
        use_search: bool = False,
        spacing: bool = True,
    ) -> str | None:
        """Return the next scripted response (expected: a ``Choice.value`` or ``None``)."""
        result: str | None = self._next(message)
        return result

    def checkbox(
        self, message: str, choices: Sequence[Choice | Separator]
    ) -> list[str] | None:
        """Return the next scripted response (expected: a list of ``Choice.value``s or ``None``)."""
        result: list[str] | None = self._next(message)
        return result

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Callable[[str], bool | str] | None = None,
    ) -> str | None:
        """Return the next scripted response (expected: a string or ``None``)."""
        result: str | None = self._next(message)
        return result

    def confirm(self, message: str, *, default: bool = False) -> bool:
        """Return the next scripted response, coerced to ``bool``."""
        return bool(self._next(message))

    def path(self, message: str, *, only_directories: bool = False) -> str | None:
        """Return the next scripted response (expected: a path string or ``None``)."""
        result: str | None = self._next(message)
        return result

    def pause(self, message: str = "Press Enter to continue...") -> None:
        """No-op: a scripted test never waits on a human to press Enter."""
        return None

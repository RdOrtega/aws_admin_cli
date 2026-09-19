"""``Flow``: the port every domain menu screen (S3, IAM, VPC, EC2, Stack) implements.

A flow owns exactly one top-level TUI screen: it is built once, given its
``AppContext`` and a ``Prompter``, and its ``.menu()`` is called repeatedly
by a ``NavigationStack`` (never by the flow calling itself, or another
``NavigationStack``, recursively -- see ``presentation/tui/navigation.py``).
Everything a flow does inside one ``.menu()`` call -- including its own
internal sub-screens, e.g. IAM's user/policy/role sub-menus -- is ordinary
sequential code built from ``Prompter`` calls and
``presentation.wiring.build_*_use_cases(ctx)``; a flow never imports
``boto3``/``botocore`` directly, and never replicates a guard rail's
condition (``force``/``allow_wildcard``/``confirm_large``) -- it only
catches the ``ValidationError`` the use case already raises and offers to
retry with the escape hatch. See ``docs/architecture.md``.

``Flow`` is a ``Protocol``, not an ABC: nothing here needs a shared base
class to reuse code from (there is no shared behavior to inherit --
``NavigationStack.push``/``.run()`` already owns the only behavior common to
every flow), so a plain structural contract is all ``flows/registry.py``
and ``NavigationStack`` need to hold a flow at arm's length.
"""

from typing import ClassVar, Protocol, Self, runtime_checkable

from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter

__all__ = ["Flow"]


@runtime_checkable
class Flow(Protocol):
    """One top-level TUI screen.

    Attributes:
        title: What this flow is called in the main menu (see
            ``presentation/tui/app.py``) -- a ``ClassVar`` because the main
            menu reads every registered flow's title to build its own
            selection list BEFORE instantiating any of them.
    """

    title: ClassVar[str]

    def __init__(self: Self, ctx: AppContext, prompter: Prompter) -> None:
        """Wire this flow to its use cases (via ``presentation.wiring``) and its ``Prompter``."""
        ...  # pragma: no cover -- Protocol body, never executed

    def menu(self: Self) -> NavAction:
        """Show this flow's own top-level menu once; return what navigation does next."""
        ...  # pragma: no cover -- Protocol body, never executed

"""The ``@aws_error_handler`` decorator: a flow-level outer safety net for AWS errors.

Every gateway call already goes through ``infrastructure.aws.error_mapper.
aws_error_boundary``, which translates raw botocore exceptions
(``ClientError``, ``EndpointConnectionError``/``ConnectTimeoutError``,
``NoCredentialsError``, ``ParamValidationError``) into this project's own
``AwsAdminCliError`` hierarchy -- a flow module never sees, and never
imports, ``botocore`` directly (see
``tests/unit/architecture/test_tui_boundaries.py``). This decorator sits one
layer above that: applied to a flow's ``.menu()`` entry point, it catches
whatever ``AwsAdminCliError`` escapes every fine-grained try/except already
inside that call tree (e.g. ``ec2_flow.py``'s own force-retry handling of
``ValidationError``, or a gateway's own ``NoSuchLifecycleConfiguration``
fallback) -- Python's normal exception propagation means an inner handler
always gets first refusal, so this never shadows one. It renders a
categorized Rich Panel, pauses on an explicit acknowledgement, then returns
``NavAction.STAY`` so ``NavigationStack`` redisplays the SAME flow instead of
the process crashing.

``EndpointUnavailableError`` (a local endpoint, e.g. LocalStack, refusing the
connection outright) is deliberately let through uncaught: ``NavigationStack``
already has its own dedicated recovery ceremony for that specific subclass
(see ``navigation.py::_handle_endpoint_unavailable``), and duplicating it here
would just be a second, slightly different banner for the exact same event.
Every OTHER ``AwsAdminCliError`` -- including the sibling
``ServiceUnavailableError`` (a real, unreachable AWS endpoint) -- is handled
here first, so ``NavigationStack``'s own generic plain-text fallback is now
only ever reached by a flow that isn't decorated yet.
"""

from collections.abc import Callable
from functools import wraps
from typing import Protocol, TypeVar

from rich.panel import Panel

from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.exceptions import (
    AccessDeniedError,
    AwsAdminCliError,
    AwsError,
    EndpointUnavailableError,
    MissingCredentialsError,
    ServiceUnavailableError,
    ValidationError,
)
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter

__all__ = ["aws_error_handler"]

_PANEL_TITLE = "AWS Error"
_RETURN_PROMPT = "Press Enter to return..."


class _MenuOwner(Protocol):
    """The structural shape ``aws_error_handler`` needs from ``self``.

    Every flow dataclass (``AuditFlow``, ``S3Flow``, ``Ec2Flow``, ``IamFlow``,
    ...) already declares both fields, so this needs no import of any actual
    flow class -- and stays reusable for whichever flow adopts the decorator
    next.
    """

    ctx: AppContext
    prompter: Prompter


_M = TypeVar("_M", bound=_MenuOwner)


def _classify(exc: AwsAdminCliError) -> tuple[str, str, str]:
    """(icon, title, body) for the Rich Panel -- most-specific exception type first.

    ``ServiceUnavailableError`` is checked (and handled) here even though
    ``EndpointUnavailableError`` is one of its subclasses -- callers filter
    that subclass out before ever reaching this function (see the module
    docstring), so by the time an exception lands here, it's always the
    "real AWS endpoint unreachable" case.
    """
    if isinstance(exc, ServiceUnavailableError):
        return (
            "🔌",
            "Connection Error",
            "Cannot connect to AWS/LocalStack endpoint. Verify your service is running.",
        )
    if isinstance(exc, AccessDeniedError | MissingCredentialsError):
        return (
            "🔑",
            "Authentication Error",
            "Invalid credentials or insufficient IAM permissions.",
        )
    if isinstance(exc, ValidationError):
        return ("⚠️", "Validation Error", "Invalid parameter passed to AWS API.")
    if isinstance(exc, AwsError):
        return ("❌", f"AWS Error ({exc.aws_code or 'Unknown'})", exc.message)
    return ("❌", "Error", exc.message)


def _render_error_panel(ctx: AppContext, exc: AwsAdminCliError) -> None:
    icon, title, body = _classify(exc)
    ctx.err_console.print(
        Panel(f"[bold red]{icon} {title}:[/] {body}", title=_PANEL_TITLE, border_style="red")
    )
    if exc.hint:
        ctx.err_console.print(f"[yellow]{exc.hint}[/]")


def aws_error_handler(func: Callable[[_M], NavAction]) -> Callable[[_M], NavAction]:
    """Wrap a flow's ``.menu()``: render a Rich Panel for any escaped AWS error.

    Returns ``NavAction.STAY`` on a caught error -- the flow's OWN menu is
    shown again next, exactly the "safely returns to the corresponding
    sub-menu" contract every other ``.menu()`` return already follows,
    never the process terminating.
    """

    @wraps(func)
    def wrapper(self: _M) -> NavAction:
        try:
            return func(self)
        except EndpointUnavailableError:
            raise
        except AwsAdminCliError as exc:
            _render_error_panel(self.ctx, exc)
            self.prompter.pause(_RETURN_PROMPT)
            return NavAction.STAY

    return wrapper

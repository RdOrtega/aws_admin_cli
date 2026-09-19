"""Tests for the ``@aws_error_handler`` decorator: a flow-level outer safety net.

Two levels are exercised:

* The decorator's own classification/rendering logic, directly, against
  every domain exception category (``ServiceUnavailableError``,
  ``AccessDeniedError``/``MissingCredentialsError``, ``ValidationError``, a
  generic ``AwsError``) plus its two deliberate non-catches
  (``EndpointUnavailableError``, and any plain non-``AwsAdminCliError`` bug).
* The full pipeline: a raw botocore exception (``EndpointConnectionError``,
  ``ClientError`` with ``AccessDenied``, ``NoCredentialsError``,
  ``ParamValidationError``) passing through
  ``infrastructure.aws.error_mapper.aws_error_boundary`` -- the same
  translation every real gateway call goes through -- and then through the
  decorator, proving end to end that none of these ever reaches the user as
  an uncaught stacktrace.
"""

import io
from dataclasses import dataclass, replace

import pytest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.exceptions import (
    AccessDeniedError,
    EndpointUnavailableError,
    MissingCredentialsError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from aws_admin_cli.infrastructure.aws.error_mapper import aws_error_boundary
from aws_admin_cli.presentation.tui.flows.error_handler import aws_error_handler
from aws_admin_cli.presentation.tui.navigation import NavAction
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ParamValidationError,
)
from rich.console import Console

from tests.fakes.prompter import FakePrompter


def _ctx() -> AppContext:
    base = AppContext.build(Settings(profile="testprofile"))
    err_console = Console(file=io.StringIO(), force_terminal=False, width=200)
    return replace(base, err_console=err_console)


class _PausingPrompter(FakePrompter):
    """Records every ``pause()`` call -- ``FakePrompter`` itself is a no-op there."""

    def __init__(self) -> None:
        super().__init__()
        self.paused: list[str] = []

    def pause(self, message: str = "Press Enter to continue...") -> None:
        self.paused.append(message)


@dataclass
class _RaisingFlow:
    """A minimal ``_MenuOwner``-shaped flow whose ``menu()`` raises a canned exception."""

    ctx: AppContext
    prompter: FakePrompter
    to_raise: BaseException

    @aws_error_handler
    def menu(self) -> NavAction:
        raise self.to_raise


@dataclass
class _GatewayCallingFlow:
    """Same shape, but raises a raw botocore exception from inside ``aws_error_boundary`` --
    exercising the full translation pipeline, not just the decorator in isolation."""

    ctx: AppContext
    prompter: FakePrompter
    boto_exc: BaseException

    @aws_error_handler
    def menu(self) -> NavAction:
        with aws_error_boundary("ec2", "DescribeInstances"):
            raise self.boto_exc


# -- Decorator classification, directly (domain exceptions) -------------------------


@pytest.mark.parametrize(
    ("exc", "expected_snippet"),
    [
        (
            ServiceUnavailableError("boom", service="ec2", operation="DescribeInstances"),
            "🔌 Connection Error: Cannot connect to AWS/LocalStack endpoint. "
            "Verify your service is running.",
        ),
        (
            AccessDeniedError("denied", service="iam", operation="CreateUser"),
            "🔑 Authentication Error: Invalid credentials or insufficient IAM permissions.",
        ),
        (
            MissingCredentialsError("no creds"),
            "🔑 Authentication Error: Invalid credentials or insufficient IAM permissions.",
        ),
        (
            ValidationError("bad param", service="ec2", operation="RunInstances"),
            "⚠️ Validation Error: Invalid parameter passed to AWS API.",
        ),
        (
            ResourceNotFoundError(
                "gone", service="s3", operation="HeadBucket", aws_code="NoSuchBucket"
            ),
            "❌ AWS Error (NoSuchBucket): gone",
        ),
    ],
)
def test_aws_error_handler_renders_the_expected_panel_and_stays(
    exc: BaseException, expected_snippet: str
) -> None:
    ctx = _ctx()
    prompter = _PausingPrompter()
    flow = _RaisingFlow(ctx=ctx, prompter=prompter, to_raise=exc)

    action = flow.menu()

    assert action is NavAction.STAY
    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert expected_snippet in output
    assert prompter.paused == ["Press Enter to return..."]


def test_aws_error_handler_prints_the_hint_when_the_exception_carries_one() -> None:
    ctx = _ctx()
    prompter = _PausingPrompter()
    exc = ValidationError("bad param", hint="Fix the parameter and retry.")
    flow = _RaisingFlow(ctx=ctx, prompter=prompter, to_raise=exc)

    flow.menu()

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "Fix the parameter and retry." in output


# -- Deliberate non-catches -----------------------------------------------------------


def test_aws_error_handler_lets_endpoint_unavailable_error_propagate_untouched() -> None:
    """NavigationStack owns the dedicated Docker/LocalStack recovery ceremony for this
    specific subclass -- the decorator must never shadow it."""
    ctx = _ctx()
    prompter = _PausingPrompter()
    exc = EndpointUnavailableError("down", endpoint_url="http://localhost:4566")
    flow = _RaisingFlow(ctx=ctx, prompter=prompter, to_raise=exc)

    with pytest.raises(EndpointUnavailableError):
        flow.menu()

    assert prompter.paused == []
    assert ctx.err_console.file.getvalue() == ""  # type: ignore[attr-defined]


def test_aws_error_handler_never_catches_a_non_aws_admin_cli_error() -> None:
    """An unexpected bug must stay loud, exactly like every other uncaught exception."""
    ctx = _ctx()
    prompter = _PausingPrompter()
    flow = _RaisingFlow(ctx=ctx, prompter=prompter, to_raise=RuntimeError("bug"))

    with pytest.raises(RuntimeError):
        flow.menu()


# -- Full pipeline: raw botocore exceptions, mocked -----------------------------------


def test_full_pipeline_endpoint_connection_error_renders_connection_panel() -> None:
    ctx = _ctx()
    prompter = _PausingPrompter()
    boto_exc = EndpointConnectionError(endpoint_url="https://ec2.us-east-1.amazonaws.com")
    flow = _GatewayCallingFlow(ctx=ctx, prompter=prompter, boto_exc=boto_exc)

    action = flow.menu()

    assert action is NavAction.STAY
    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert (
        "🔌 Connection Error: Cannot connect to AWS/LocalStack endpoint. "
        "Verify your service is running." in output
    )


def test_full_pipeline_client_error_access_denied_renders_authentication_panel() -> None:
    ctx = _ctx()
    prompter = _PausingPrompter()
    boto_exc = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "DescribeInstances"
    )
    flow = _GatewayCallingFlow(ctx=ctx, prompter=prompter, boto_exc=boto_exc)

    action = flow.menu()

    assert action is NavAction.STAY
    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "🔑 Authentication Error: Invalid credentials or insufficient IAM permissions." in output


def test_full_pipeline_no_credentials_error_renders_authentication_panel() -> None:
    ctx = _ctx()
    prompter = _PausingPrompter()
    flow = _GatewayCallingFlow(ctx=ctx, prompter=prompter, boto_exc=NoCredentialsError())

    action = flow.menu()

    assert action is NavAction.STAY
    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "🔑 Authentication Error: Invalid credentials or insufficient IAM permissions." in output


def test_full_pipeline_param_validation_error_renders_validation_panel() -> None:
    ctx = _ctx()
    prompter = _PausingPrompter()
    boto_exc = ParamValidationError(report="Missing required parameter InstanceType")
    flow = _GatewayCallingFlow(ctx=ctx, prompter=prompter, boto_exc=boto_exc)

    action = flow.menu()

    assert action is NavAction.STAY
    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "⚠️ Validation Error: Invalid parameter passed to AWS API." in output


def test_full_pipeline_generic_client_error_shows_the_code_and_message() -> None:
    boto_exc = ClientError(
        {"Error": {"Code": "Unknown", "Message": "something odd"}}, "DescribeInstances"
    )
    ctx = _ctx()
    prompter = _PausingPrompter()
    flow = _GatewayCallingFlow(ctx=ctx, prompter=prompter, boto_exc=boto_exc)

    action = flow.menu()

    assert action is NavAction.STAY
    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "AWS Error (Unknown): something odd" in output


def test_full_pipeline_never_lets_a_stacktrace_escape_for_any_mocked_error() -> None:
    """No matter which of the four mocked errors fires, ``menu()`` returns normally."""
    for boto_exc in (
        EndpointConnectionError(endpoint_url="https://ec2.us-east-1.amazonaws.com"),
        ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "Op"),
        NoCredentialsError(),
        ParamValidationError(report="bad"),
    ):
        flow = _GatewayCallingFlow(ctx=_ctx(), prompter=_PausingPrompter(), boto_exc=boto_exc)
        assert flow.menu() is NavAction.STAY

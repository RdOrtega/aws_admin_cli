"""Tests for ``LambdaFlow``: the basic "AWS Lambda & Serverless" scaffolding screen."""

import io
import json
import zipfile
from dataclasses import replace

import boto3
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.flows.lambda_flow import LambdaFlow
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice
from aws_admin_cli.presentation.tui.navigation import NavAction
from moto import mock_aws
from rich.console import Console

from tests.fakes.prompter import FakePrompter

_ASSUME_ROLE_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)


def _app_ctx() -> AppContext:
    return AppContext.build(Settings(profile="testprofile"))


def _capture_console(ctx: AppContext) -> AppContext:
    return replace(ctx, console=Console(file=io.StringIO(), force_terminal=False, width=200))


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("lambda_function.py", "def handler(event, context):\n    return event\n")
    return buf.getvalue()


def _create_test_function(region: str, name: str) -> None:
    """Create a minimal, real (moto) Lambda function in ``region``."""
    role_arn = boto3.client("iam", region_name=region).create_role(
        RoleName=f"{name}-role", AssumeRolePolicyDocument=_ASSUME_ROLE_POLICY
    )["Role"]["Arn"]
    boto3.client("lambda", region_name=region).create_function(
        FunctionName=name,
        Runtime="python3.12",
        Role=role_arn,
        Handler="lambda_function.handler",
        Code={"ZipFile": _zip_bytes()},
        Environment={"Variables": {"STAGE": "test"}},
    )


# -- Root menu shape and navigation --------------------------------------------------


def test_root_choices_match_the_lambda_menu_template() -> None:
    ctx = _app_ctx()
    choices = LambdaFlow(ctx, FakePrompter())._choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "🔎 List Functions (All Regions)",
        "─" * 66,
        "↩️  Back to Main Menu",
    ]


def test_root_menu_cancelled_exits() -> None:
    assert LambdaFlow(_app_ctx(), FakePrompter([None])).menu() is NavAction.EXIT


def test_root_menu_back_returns_back() -> None:
    assert LambdaFlow(_app_ctx(), FakePrompter([NAV_BACK])).menu() is NavAction.BACK


def test_root_menu_exit_returns_exit() -> None:
    assert LambdaFlow(_app_ctx(), FakePrompter([NAV_EXIT])).menu() is NavAction.EXIT


# -- List Functions (All Regions) -----------------------------------------------------


@mock_aws
def test_list_functions_with_none_shows_empty_state_and_returns() -> None:
    ctx = _capture_console(_app_ctx())

    prompter = FakePrompter(["list_functions"])
    action = LambdaFlow(ctx, prompter).menu()

    assert action is NavAction.STAY


@mock_aws
def test_list_functions_shows_a_function_created_in_the_active_region() -> None:
    ctx = _capture_console(_app_ctx())  # active session region: us-east-1
    _create_test_function("us-east-1", "local-fn")

    prompter = FakePrompter(["list_functions", NAV_BACK])
    action = LambdaFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "local-fn" in output
    assert "us-east-1" in output
    assert "python3.12" in output


@mock_aws
def test_list_functions_includes_a_function_from_a_foreign_region() -> None:
    """The scan is global -- a function outside the active session region still shows up."""
    ctx = _capture_console(_app_ctx())  # active session region: us-east-1
    _create_test_function("us-west-2", "foreign-fn")

    prompter = FakePrompter(["list_functions", NAV_BACK])
    action = LambdaFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert ctx.settings.region == "us-east-1"
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "foreign-fn" in output
    assert "us-west-2" in output


@mock_aws
def test_selecting_a_function_shows_its_runtime_and_environment_variables() -> None:
    ctx = _capture_console(_app_ctx())
    _create_test_function("us-east-1", "detail-fn")

    prompter = FakePrompter(["list_functions", "detail-fn", NAV_BACK])
    action = LambdaFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "detail-fn" in output
    assert "python3.12" in output
    assert "lambda_function.handler" in output
    assert "STAGE=test" in output

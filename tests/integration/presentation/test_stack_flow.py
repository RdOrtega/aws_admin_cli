"""Tests for ``StackFlow``: plan, apply (with confirmation), status, and destroy."""

from pathlib import Path

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.flows.stack_flow import StackFlow
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT
from aws_admin_cli.presentation.tui.navigation import NavAction
from moto import mock_aws

from tests.fakes.prompter import FakePrompter

_MANIFEST_YAML = """\
apiVersion: v1
name: tui-test-stack
resources:
  - id: app-bucket
    kind: s3:bucket
    properties:
      bucket_name: tui-test-stack-bucket
"""


def _app_ctx() -> AppContext:
    return AppContext.build(Settings(profile="testprofile"))


def _write_manifest(tmp_path: Path) -> Path:
    manifest_path = tmp_path / "stack.yaml"
    manifest_path.write_text(_MANIFEST_YAML, encoding="utf-8")
    return manifest_path


@mock_aws
def test_plan_renders_and_pauses_without_touching_aws(tmp_path: Path) -> None:
    ctx = _app_ctx()
    manifest_path = _write_manifest(tmp_path)
    prompter = FakePrompter(["plan", str(manifest_path)])

    action = StackFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert not ctx.client_factory.s3().list_buckets()["Buckets"]


@mock_aws
def test_apply_confirmed_creates_the_bucket(tmp_path: Path) -> None:
    ctx = _app_ctx()
    manifest_path = _write_manifest(tmp_path)
    prompter = FakePrompter(["apply", str(manifest_path), "yes"])

    action = StackFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = [b["Name"] for b in ctx.client_factory.s3().list_buckets()["Buckets"]]
    assert "tui-test-stack-bucket" in buckets


@mock_aws
def test_apply_declined_never_touches_aws(tmp_path: Path) -> None:
    ctx = _app_ctx()
    manifest_path = _write_manifest(tmp_path)
    prompter = FakePrompter(["apply", str(manifest_path), "no"])

    action = StackFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert not ctx.client_factory.s3().list_buckets()["Buckets"]


@mock_aws
def test_destroy_after_apply_removes_the_bucket(tmp_path: Path) -> None:
    ctx = _app_ctx()
    manifest_path = _write_manifest(tmp_path)
    StackFlow(ctx, FakePrompter(["apply", str(manifest_path), "yes"])).menu()

    prompter = FakePrompter(["destroy", "tui-test-stack", "yes"])
    action = StackFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = [b["Name"] for b in ctx.client_factory.s3().list_buckets()["Buckets"]]
    assert "tui-test-stack-bucket" not in buckets


@mock_aws
def test_status_with_no_stacks_short_circuits_before_pick() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter(["status"])

    action = StackFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert prompter.asked == ["Stack -- what do you want to do?"]


@mock_aws
def test_status_cancel_at_pick_stack_returns_without_calling_engine(tmp_path: Path) -> None:
    ctx = _app_ctx()
    manifest_path = _write_manifest(tmp_path)
    StackFlow(ctx, FakePrompter(["apply", str(manifest_path), "yes"])).menu()

    prompter = FakePrompter(["status", NAV_BACK])
    action = StackFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = [b["Name"] for b in ctx.client_factory.s3().list_buckets()["Buckets"]]
    assert "tui-test-stack-bucket" in buckets


@mock_aws
def test_destroy_cancel_at_pick_stack_leaves_resources_untouched(tmp_path: Path) -> None:
    ctx = _app_ctx()
    manifest_path = _write_manifest(tmp_path)
    StackFlow(ctx, FakePrompter(["apply", str(manifest_path), "yes"])).menu()

    prompter = FakePrompter(["destroy", NAV_BACK])
    action = StackFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    buckets = [b["Name"] for b in ctx.client_factory.s3().list_buckets()["Buckets"]]
    assert "tui-test-stack-bucket" in buckets


@mock_aws
def test_plan_cancelled_manifest_path_short_circuits() -> None:
    ctx = _app_ctx()
    prompter = FakePrompter(["plan", None])

    action = StackFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert prompter.asked == ["Stack -- what do you want to do?", "Manifest path:"]
    assert not ctx.client_factory.s3().list_buckets()["Buckets"]


def test_menu_back_returns_back() -> None:
    action = StackFlow(_app_ctx(), FakePrompter([NAV_BACK])).menu()
    assert action is NavAction.BACK


def test_menu_exit_returns_exit() -> None:
    action = StackFlow(_app_ctx(), FakePrompter([NAV_EXIT])).menu()
    assert action is NavAction.EXIT

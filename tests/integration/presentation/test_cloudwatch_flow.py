"""Tests for ``CloudWatchFlow``: the "CloudWatch Observability & Logs" screen.

Split out of ``test_audit_flow.py`` alongside the ``cloudwatch_flow.py`` /
``audit_flow.py`` module split -- these are the exact same Alarms &
Monitoring Governance / Idle Compute Instances tests that used to live there,
adjusted only for one less menu-nesting level (no more "ec2_audit" ->
"EC2 Compute & CloudWatch Insights" wrapper picker in between): this screen
IS the CloudWatch domain now, not a sub-menu of a combined audit.
"""

import io
from dataclasses import replace

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.flows.cloudwatch_flow import CloudWatchFlow
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.wiring import build_cloudwatch_use_cases
from moto import mock_aws
from rich.console import Console

from tests.fakes.prompter import FakePrompter


def _app_ctx() -> AppContext:
    return AppContext.build(Settings(profile="testprofile"))


def _capture_console(ctx: AppContext) -> AppContext:
    """A context whose ``console`` writes to an in-memory buffer, readable back."""
    return replace(ctx, console=Console(file=io.StringIO(), force_terminal=False, width=200))


def _any_ami_id(ctx: AppContext) -> str:
    images = ctx.client_factory.ec2().describe_images(Owners=["amazon"])["Images"]
    return str(images[0]["ImageId"])


def _launch_raw_instance(ctx: AppContext) -> str:
    """Launch a plain instance straight through boto3 -- bypasses ``ec2_flow.py``'s wizard."""
    response = ctx.client_factory.ec2().run_instances(
        ImageId=_any_ami_id(ctx), MinCount=1, MaxCount=1, InstanceType="t3.micro"
    )
    return str(response["Instances"][0]["InstanceId"])


# -- Root menu shape and navigation --------------------------------------------------


def test_root_choices_match_the_cloudwatch_menu_template() -> None:
    ctx = _app_ctx()
    choices = CloudWatchFlow(ctx, FakePrompter())._choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "🛡️ Alarms & Monitoring Governance",
        "💤 Idle Compute Instances (Low CPU > 15 days)",
        "📈 Live Metrics & Log Diagnostics [Pending]",
        "─" * 66,
        "↩️  Back to Main Menu",
    ]
    pending = next(
        c for c in choices if isinstance(c, Choice) and c.value == "live_metrics_pending"
    )
    assert pending.disabled


def test_root_menu_cancelled_exits() -> None:
    assert CloudWatchFlow(_app_ctx(), FakePrompter([None])).menu() is NavAction.EXIT


def test_root_menu_back_returns_back() -> None:
    assert CloudWatchFlow(_app_ctx(), FakePrompter([NAV_BACK])).menu() is NavAction.BACK


def test_root_menu_exit_returns_exit() -> None:
    assert CloudWatchFlow(_app_ctx(), FakePrompter([NAV_EXIT])).menu() is NavAction.EXIT


# -- Alarms & Monitoring Governance -----------------------------------------------------


@mock_aws
def test_alarms_governance_menu_opens_and_backs_out_cleanly() -> None:
    prompter = FakePrompter(["alarms_governance", NAV_BACK])
    action = CloudWatchFlow(_app_ctx(), prompter).menu()

    assert action is NavAction.STAY
    assert "Alarms & Monitoring Governance -- choose a view:" in prompter.asked


@mock_aws
def test_monitored_instances_view_lists_an_instance_with_an_active_alarm() -> None:
    ctx = _capture_console(_app_ctx())
    instance_id = _launch_raw_instance(ctx)
    build_cloudwatch_use_cases(ctx).create_cpu_alarm.execute(instance_id)

    prompter = FakePrompter(["alarms_governance", "monitored_instances", NAV_BACK])
    action = CloudWatchFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "1 of 1 active instances have CloudWatch alarms." in output
    assert instance_id in output


@mock_aws
def test_unmonitored_instances_view_shows_the_header_and_the_unprotected_instance() -> None:
    ctx = _capture_console(_app_ctx())
    instance_id = _launch_raw_instance(ctx)

    prompter = FakePrompter(["alarms_governance", "unmonitored_instances", NAV_BACK, NAV_BACK])
    action = CloudWatchFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 1 active instances without CloudWatch Alarms." in output
    assert instance_id in output


@mock_aws
def test_attach_cpu_alarm_action_creates_the_alarm() -> None:
    ctx = _app_ctx()
    instance_id = _launch_raw_instance(ctx)

    prompter = FakePrompter(
        [
            "alarms_governance",
            "unmonitored_instances",
            instance_id,
            "attach_cpu_alarm",
            NAV_BACK,
        ]
    )
    action = CloudWatchFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    alarms = build_cloudwatch_use_cases(ctx).list_alarms.execute()
    assert any(
        a.dimension_value("InstanceId") == instance_id and a.metric_name == "CPUUtilization"
        for a in alarms
    )


@mock_aws
def test_attach_status_check_alarm_action_creates_the_alarm() -> None:
    ctx = _app_ctx()
    instance_id = _launch_raw_instance(ctx)

    prompter = FakePrompter(
        [
            "alarms_governance",
            "unmonitored_instances",
            instance_id,
            "attach_status_check_alarm",
            NAV_BACK,
        ]
    )
    action = CloudWatchFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    alarms = build_cloudwatch_use_cases(ctx).list_alarms.execute()
    assert any(
        a.dimension_value("InstanceId") == instance_id and a.metric_name == "StatusCheckFailed"
        for a in alarms
    )


# -- Idle Compute Instances (Low CPU > 15 days) ------------------------------------------


@mock_aws
def test_idle_compute_instances_flags_a_running_instance_with_no_cloudwatch_data() -> None:
    ctx = _capture_console(_app_ctx())
    instance_id = _launch_raw_instance(ctx)

    prompter = FakePrompter(["idle_compute"])
    action = CloudWatchFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 1 idle instances" in output
    assert instance_id in output
    assert "No data" in output

"""Tests for Global Region Context propagation: ClientFactory, gateways, and TUI flows.

``AppContext.set_region`` (the Region Selector's own mechanism -- see
``environment_flow.py``) mutates the shared ``Settings.region`` in place and
clears ``ClientFactory``'s client cache, so every gateway built from this
context picks up the new region on its very next call, with no CLI restart
required. ``test_client_factory.py`` already locks in the ``region_name``
propagation at the ``ClientFactory`` unit level (fake session/client
doubles); this file exercises the same guarantee end-to-end against real
(moto) EC2/S3/CloudWatch/IAM backends.

No test here duplicates two guards that already exist elsewhere:
``test_s3_flow.py::test_create_bucket_inherits_the_global_region_with_no_region_prompt``
(a bucket always lands in ``ctx.settings.region``, no region prompt, no
scripted response left over for one) and
``test_ec2_flow.py::test_render_launch_summary_shows_the_sessions_global_region_with_no_prompt``
(the launch wizard's Region row reflects the switch, with a bare
``FakePrompter()`` proving zero prompts are ever asked for it). This file
adds the same "no redundant region prompt" guard for ``audit_flow.py``,
which isn't covered by either of those.
"""

import io
from dataclasses import replace

from aws_admin_cli.application.dto.ec2 import ListInstancesRequest
from aws_admin_cli.application.dto.iam import CreateUserRequest
from aws_admin_cli.application.dto.s3 import CreateBucketRequest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.flows._shared import render_header
from aws_admin_cli.presentation.tui.flows.audit_flow import AuditFlow
from aws_admin_cli.presentation.tui.flows.environment_flow import EnvironmentFlow
from aws_admin_cli.presentation.tui.menu import NAV_BACK, plain_text
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.wiring import (
    build_cloudwatch_use_cases,
    build_ec2_use_cases,
    build_iam_use_cases,
    build_s3_use_cases,
)
from moto import mock_aws
from rich.console import Console

from tests.fakes.prompter import FakePrompter

_REGION_SELECT_PROMPT = "Select a region:"


def _ctx(region: str = "us-east-1") -> AppContext:
    return AppContext.build(Settings(profile="testprofile", region=region))


def _ctx_with_captured_output(region: str = "us-east-1") -> AppContext:
    """Same wiring as ``_ctx``, but with ``err_console`` redirected to a buffer."""
    base = _ctx(region)
    return replace(base, err_console=Console(file=io.StringIO(), force_terminal=False, width=200))


class _SpyPrompter(FakePrompter):
    """Records the ``choices`` list a ``select()`` call was offered, not just the message."""

    def __init__(self, responses: list[object]) -> None:
        super().__init__(responses)
        self.offered_choices: list[object] | None = None

    def select(  # type: ignore[override]
        self, message, choices, *, default=None, use_search=False, spacing=True
    ):
        self.offered_choices = list(choices)
        return super().select(
            message, choices, default=default, use_search=use_search, spacing=spacing
        )


def _any_ami_id(ctx: AppContext) -> str:
    images = ctx.client_factory.ec2().describe_images(Owners=["amazon"])["Images"]
    return str(images[0]["ImageId"])


def _launch_instance(ctx: AppContext) -> str:
    response = ctx.client_factory.ec2().run_instances(
        ImageId=_any_ami_id(ctx), MinCount=1, MaxCount=1, InstanceType="t3.micro"
    )
    return str(response["Instances"][0]["InstanceId"])


# -- ClientFactory / gateway injection: every service, instantly, no restart ---------


@mock_aws
def test_switching_region_rebuilds_every_gateways_client_with_the_new_region() -> None:
    """Every service client is rebuilt (not reused stale) the moment the region switches."""
    ctx = _ctx("us-east-1")
    ec2 = ctx.client_factory.ec2()
    s3 = ctx.client_factory.s3()
    cloudwatch = ctx.client_factory.cloudwatch()
    iam = ctx.client_factory.iam()
    assert ec2.meta.region_name == "us-east-1"
    assert s3.meta.region_name == "us-east-1"
    assert cloudwatch.meta.region_name == "us-east-1"

    ctx.set_region("eu-west-1")

    new_ec2 = ctx.client_factory.ec2()
    new_s3 = ctx.client_factory.s3()
    new_cloudwatch = ctx.client_factory.cloudwatch()
    new_iam = ctx.client_factory.iam()
    assert new_ec2 is not ec2
    assert new_s3 is not s3
    assert new_cloudwatch is not cloudwatch
    assert new_iam is not iam
    assert new_ec2.meta.region_name == "eu-west-1"
    assert new_s3.meta.region_name == "eu-west-1"
    assert new_cloudwatch.meta.region_name == "eu-west-1"


# -- EC2: operations after a region switch target the new region --------------------


@mock_aws
def test_ec2_list_instances_after_region_switch_sees_only_the_new_regions_instances() -> None:
    """moto isolates EC2 state per region -- an empty result after the switch proves the
    use-case layer (not just a raw client) is really hitting the new region, not a
    cached client still pinned to the old one."""
    ctx = _ctx("us-east-1")
    _launch_instance(ctx)
    before = build_ec2_use_cases(ctx).list_instances.execute(ListInstancesRequest())
    assert len(before) == 1

    ctx.set_region("eu-west-1")

    after = build_ec2_use_cases(ctx).list_instances.execute(ListInstancesRequest())
    assert after == []


# -- S3: a bucket created after a region switch lands in the new region -------------


@mock_aws
def test_s3_bucket_created_after_region_switch_lands_in_the_new_region() -> None:
    ctx = _ctx("us-east-1")
    ctx.set_region("eu-west-1")
    use_cases = build_s3_use_cases(ctx)

    use_cases.create_bucket.execute(
        CreateBucketRequest(
            name="region-switch-bucket", region=ctx.settings.region, allow_public=False
        )
    )

    assert use_cases.gateway.get_bucket_location("region-switch-bucket") == "eu-west-1"


# -- CloudWatch: an alarm created after a region switch is scoped to the new region --


@mock_aws
def test_cloudwatch_alarm_created_after_region_switch_is_only_visible_in_the_new_region() -> None:
    ctx = _ctx("us-east-1")
    instance_id = _launch_instance(ctx)

    ctx.set_region("eu-west-1")
    build_cloudwatch_use_cases(ctx).create_cpu_alarm.execute(instance_id)

    new_region_alarms = build_cloudwatch_use_cases(ctx).list_alarms.execute()
    assert any(a.dimension_value("InstanceId") == instance_id for a in new_region_alarms)

    # A client pinned back to the OLD region must see none of it -- proof the alarm was
    # really created against eu-west-1, not just labeled as such.
    old_region_cloudwatch = ctx.client_factory.create("cloudwatch", region="us-east-1")
    assert old_region_cloudwatch.describe_alarms()["MetricAlarms"] == []


# -- IAM: a global service, gracefully unaffected by the active region --------------


@mock_aws
def test_iam_operations_succeed_after_switching_to_a_non_default_region() -> None:
    """IAM has no regional endpoints in the standard partition -- switching the global
    region context must never break (or even affect) an IAM call.
    """
    ctx = _ctx("us-east-1")
    ctx.set_region("sa-east-1")
    use_cases = build_iam_use_cases(ctx)

    use_cases.create_user.execute(CreateUserRequest(name="region-fallback-user"))

    users = use_cases.list_users.execute(None)
    assert any(u.user_name == "region-fallback-user" for u in users)


# -- No redundant "Select Region" prompt inside audit_flow.py ------------------------


@mock_aws
def test_ec2_insights_never_prompts_to_select_a_region() -> None:
    ctx = _ctx("eu-west-1")
    prompter = FakePrompter(["ec2_audit", "stale_stopped", NAV_BACK])

    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert _REGION_SELECT_PROMPT not in prompter.asked


@mock_aws
def test_s3_storage_lifecycle_audit_never_prompts_to_select_a_region() -> None:
    ctx = _ctx("sa-east-1")
    prompter = FakePrompter(["s3_audit", "s3_public_access", NAV_BACK])

    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert _REGION_SELECT_PROMPT not in prompter.asked


@mock_aws
def test_iam_security_insights_never_prompts_to_select_a_region() -> None:
    ctx = _ctx("us-west-2")
    prompter = FakePrompter(["iam_audit", NAV_BACK])

    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    assert _REGION_SELECT_PROMPT not in prompter.asked


# -- Region Context display: friendly names + active-region highlighting -------------


def test_header_region_field_shows_the_active_regions_friendly_name() -> None:
    ctx = _ctx_with_captured_output("us-west-2")

    render_header(ctx)

    output = ctx.err_console.file.getvalue()  # type: ignore[attr-defined]
    assert "us-west-2 (Oregon)" in output


def test_region_selection_prompt_shows_active_badge_only_for_the_active_region() -> None:
    """Option 1: the active region's status badge itself becomes ``[✔ ACTIVE]``
    (replacing its ``[● ONLINE]``), instead of an ``(ACTIVE)`` tag appended
    next to the label."""
    ctx = _ctx_with_captured_output("eu-west-1")
    prompter = _SpyPrompter([NAV_BACK])

    EnvironmentFlow(ctx, prompter).menu()

    offered = prompter.offered_choices
    assert offered is not None
    labels = {c.value: plain_text(c.title) for c in offered if hasattr(c, "title")}
    assert "eu-west-1 (Ireland)" in labels["eu-west-1"]
    assert "[✔ ACTIVE]" in labels["eu-west-1"]
    assert "[● ONLINE]" not in labels["eu-west-1"]
    for code, title in labels.items():
        if code != "eu-west-1":
            assert "[✔ ACTIVE]" not in title

"""Tests for ``AuditFlow``: the "Security & Compliance Audit" screen.

IAM's own IAM Security Insights view is Credential-Report-driven (see
``application/use_cases/iam/get_credential_report.py``) rather than the old
local-ledger "Audit Inactive Users" feature that used to live in
``iam_flow.py`` -- ``test_iam_flow.py`` no longer covers any of that; this
file is its replacement. EC2 Resource Hygiene and S3 Storage & Lifecycle
Audit are both NEW audits shipped directly in ``audit_flow.py`` (never
delegating into ``ec2_flow.py``/``s3_flow.py``), so their views are fully
exercised here against a real (moto) backend -- ``test_ec2_flow.py`` and
``test_s3_flow.py`` are both untouched and still cover each service's own
separate "Resource Audit"/"Audit" submenu. The CloudWatch-specific views
(Alarms & Monitoring Governance, Idle Compute Instances) that used to live
in this same module/file moved to ``cloudwatch_flow.py``/
``test_cloudwatch_flow.py``.
"""

import io
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from aws_admin_cli.application.use_cases.iam.get_credential_report import CredentialReportEntry
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.presentation.tui.flows.audit_flow import (
    AuditFlow,
    _inactivity_matches,
    _no_mfa_matches,
    _password_unchanged_matches,
)
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice
from aws_admin_cli.presentation.tui.navigation import NavAction
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
    """Launch a plain instance straight through boto3 -- deliberately NOT ``ManagedBy``-tagged.

    Bypasses ``ec2_flow.py``'s own launch wizard entirely: these tests exist
    to exercise the audit's own remediation actions (including the
    "not managed by this CLI, force anyway?" retry path), not instance
    creation, which ``test_ec2_flow.py`` already covers.
    """
    response = ctx.client_factory.ec2().run_instances(
        ImageId=_any_ami_id(ctx), MinCount=1, MaxCount=1, InstanceType="t3.micro"
    )
    return str(response["Instances"][0]["InstanceId"])


def _stop_instance(ctx: AppContext, instance_id: str) -> None:
    ctx.client_factory.ec2().stop_instances(InstanceIds=[instance_id])


def _create_bucket(ctx: AppContext, name: str) -> None:
    """Create a plain bucket straight through boto3 -- bypasses ``s3_flow.py``'s wizard."""
    ctx.client_factory.s3().create_bucket(Bucket=name)


def _entry(
    name: str,
    *,
    last_activity: datetime | None = None,
    password_last_changed: datetime | None = None,
    mfa_active: bool = False,
    password_enabled: bool = True,
) -> CredentialReportEntry:
    """Build a synthetic ``CredentialReportEntry`` -- no AWS call involved."""
    return CredentialReportEntry(
        user_name=name,
        arn=f"arn:aws:iam::123456789012:user/{name}",
        user_creation_time=None,
        password_enabled=password_enabled,
        password_last_used=last_activity,
        password_last_changed=password_last_changed,
        mfa_active=mfa_active,
        access_key_1_active=False,
        access_key_1_last_used_date=None,
        access_key_2_active=False,
        access_key_2_last_used_date=None,
    )


# -- Root menu shape and navigation --------------------------------------------------


def test_root_choices_match_the_audit_menu_template() -> None:
    ctx = _app_ctx()
    choices = AuditFlow(ctx, FakePrompter())._choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "🔐 IAM Identity & Access Audit",
        "🖥️  EC2 Resource Hygiene Audit",
        "📦 S3 Storage & Lifecycle Audit",
        "─" * 66,
        "↩️  Back to Main Menu",
    ]


def test_root_menu_cancelled_exits() -> None:
    assert AuditFlow(_app_ctx(), FakePrompter([None])).menu() is NavAction.EXIT


def test_root_menu_back_returns_back() -> None:
    assert AuditFlow(_app_ctx(), FakePrompter([NAV_BACK])).menu() is NavAction.BACK


def test_root_menu_exit_returns_exit() -> None:
    assert AuditFlow(_app_ctx(), FakePrompter([NAV_EXIT])).menu() is NavAction.EXIT


@mock_aws
def test_iam_audit_opens_the_security_insights_menu() -> None:
    prompter = FakePrompter(["iam_audit", NAV_BACK])
    action = AuditFlow(_app_ctx(), prompter).menu()

    assert action is NavAction.STAY
    assert prompter.asked == [
        "Security & Compliance Audit -- choose a domain:",
        "IAM Security Insights -- choose a view:",
    ]


@mock_aws
def test_ec2_audit_opens_the_resource_hygiene_audit_menu() -> None:
    prompter = FakePrompter(["ec2_audit", NAV_BACK])
    action = AuditFlow(_app_ctx(), prompter).menu()

    assert action is NavAction.STAY
    assert prompter.asked == [
        "Security & Compliance Audit -- choose a domain:",
        "EC2 Resource Hygiene Audit -- choose a view:",
    ]


@mock_aws
def test_s3_audit_opens_the_storage_lifecycle_audit_menu() -> None:
    prompter = FakePrompter(["s3_audit", NAV_BACK])
    action = AuditFlow(_app_ctx(), prompter).menu()

    assert action is NavAction.STAY
    assert prompter.asked == [
        "Security & Compliance Audit -- choose a domain:",
        "S3 Storage & Lifecycle Audit -- choose a view:",
    ]


# -- IAM Security Insights: menu shape -----------------------------------------------


def test_security_insights_choices_are_the_exact_specified_strings() -> None:
    ctx = _app_ctx()
    choices = AuditFlow(ctx, FakePrompter())._security_insights_choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "👤 Inactive users (> 15 days)",
        "🛑 Disabled users (> 30 days)",
        "🔑 Password unchanged (> 90 days)",
        "🛡️ Users without MFA active",
        "─" * 66,
        "↩️  Back to Audit Menu",
    ]


# -- IAM Security Insights: drill-down views (against a real Credential Report) -----


@mock_aws
def test_inactive_users_view_shows_a_never_used_user_with_the_summary_header() -> None:
    ctx = _capture_console(_app_ctx())
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(["iam_audit", "inactive_users", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 1 inactive users out of 1 total IAM accounts analyzed." in output
    assert "alice" in output


@mock_aws
def test_disabled_users_view_shows_the_same_never_used_user() -> None:
    """A never-active user is stale enough to match both the 15d and 30d tiers."""
    ctx = _capture_console(_app_ctx())
    ctx.client_factory.iam().create_user(UserName="alice")

    prompter = FakePrompter(["iam_audit", "disabled_users", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 1 disabled users out of 1 total IAM accounts analyzed." in output
    assert "alice" in output


@mock_aws
def test_password_unchanged_view_excludes_a_freshly_created_password() -> None:
    """A password changed moments ago (real Credential Report data) is nowhere near 90 days."""
    ctx = _capture_console(_app_ctx())
    ctx.client_factory.iam().create_user(UserName="alice")
    ctx.client_factory.iam().create_login_profile(
        UserName="alice", Password="Xx!23456789012", PasswordResetRequired=False
    )

    prompter = FakePrompter(["iam_audit", "password_unchanged", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 0 users with password unchanged out of 1 total IAM accounts analyzed." in output


@mock_aws
def test_no_mfa_view_excludes_a_user_with_an_enabled_mfa_device() -> None:
    ctx = _capture_console(_app_ctx())
    iam = ctx.client_factory.iam()
    iam.create_user(UserName="alice")
    iam.create_user(UserName="bob")
    device = iam.create_virtual_mfa_device(VirtualMFADeviceName="bob-device")
    iam.enable_mfa_device(
        UserName="bob",
        SerialNumber=device["VirtualMFADevice"]["SerialNumber"],
        AuthenticationCode1="123456",
        AuthenticationCode2="654321",
    )

    prompter = FakePrompter(["iam_audit", "no_mfa", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 1 users without MFA active out of 2 total IAM accounts analyzed." in output
    assert "alice" in output
    assert "bob" not in output


# -- Pure matcher functions: exact threshold behavior --------------------------------


def test_inactivity_matches_excludes_activity_within_the_threshold() -> None:
    now = datetime(2026, 2, 1, tzinfo=UTC)
    recent = _entry("recent", last_activity=now - timedelta(days=10))
    stale = _entry("stale", last_activity=now - timedelta(days=16))
    never = _entry("never", last_activity=None)

    matches = _inactivity_matches([recent, stale, never], now=now, threshold_days=15)

    assert [e.user_name for e in matches] == ["stale", "never"]


def test_inactivity_matches_boundary_is_exclusive() -> None:
    now = datetime(2026, 2, 1, tzinfo=UTC)
    exactly_at_threshold = _entry("edge", last_activity=now - timedelta(days=15))

    assert _inactivity_matches([exactly_at_threshold], now=now, threshold_days=15) == []


def test_password_unchanged_matches_excludes_users_with_no_password() -> None:
    now = datetime(2026, 2, 1, tzinfo=UTC)
    no_password = _entry("svc", password_last_changed=None)
    stale_password = _entry("alice", password_last_changed=now - timedelta(days=91))

    matches = _password_unchanged_matches(
        [no_password, stale_password], now=now, threshold_days=90
    )

    assert [e.user_name for e in matches] == ["alice"]


def test_no_mfa_matches_excludes_only_users_with_mfa_active() -> None:
    with_mfa = _entry("bob", mfa_active=True)
    without_mfa = _entry("alice", mfa_active=False)

    matches = _no_mfa_matches([with_mfa, without_mfa])

    assert [e.user_name for e in matches] == ["alice"]


# -- EC2 Resource Hygiene Audit: menu shape --------------------------------------------


def test_ec2_insights_choices_match_the_exact_specified_strings() -> None:
    ctx = _app_ctx()
    choices = AuditFlow(ctx, FakePrompter())._ec2_insights_choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "🛑 Stale Stopped Instances (> 30 days)",
        "⚡ Automated Start/Stop Schedulers (FinOps) [Pending]",
        "─" * 66,
        "↩️  Back to Audit Menu",
    ]
    pending = next(
        c for c in choices if isinstance(c, Choice) and c.value == "schedulers_pending"
    )
    assert pending.disabled


# -- EC2: Stale Stopped Instances (> 30 days) -----------------------------------------


@mock_aws
def test_stale_stopped_instances_with_none_shows_the_header_and_returns() -> None:
    ctx = _capture_console(_app_ctx())
    prompter = FakePrompter(["ec2_audit", "stale_stopped", NAV_BACK])

    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert (
        "Found 0 stopped instances inactive for > 30 days. "
        "Releasing orphaned EBS storage recommended." in output
    )


@mock_aws
def test_stale_stopped_instances_lists_a_stopped_instance() -> None:
    ctx = _capture_console(_app_ctx())
    instance_id = _launch_raw_instance(ctx)
    _stop_instance(ctx, instance_id)

    prompter = FakePrompter(["ec2_audit", "stale_stopped", NAV_BACK, NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 1 stopped instances inactive for > 30 days." in output
    assert instance_id in output


@mock_aws
def test_stale_stopped_terminate_force_terminates_an_unmanaged_instance() -> None:
    """Not ``ManagedBy``-tagged -- the plain terminate is refused, then retried with force."""
    ctx = _app_ctx()
    instance_id = _launch_raw_instance(ctx)
    _stop_instance(ctx, instance_id)

    prompter = FakePrompter(
        [
            "ec2_audit",
            "stale_stopped",
            instance_id,
            "terminate_instance",
            "yes",  # confirm_destructive
            "yes",  # "not managed by this CLI, terminate anyway (--force)?"
            NAV_BACK,
        ]
    )
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    state = ctx.client_factory.ec2().describe_instances(InstanceIds=[instance_id])[
        "Reservations"
    ][0]["Instances"][0]["State"]["Name"]
    assert state in ("shutting-down", "terminated")


@mock_aws
def test_stale_stopped_snapshot_and_terminate_creates_a_snapshot_first() -> None:
    ctx = _capture_console(_app_ctx())
    instance_id = _launch_raw_instance(ctx)
    _stop_instance(ctx, instance_id)

    prompter = FakePrompter(
        [
            "ec2_audit",
            "stale_stopped",
            instance_id,
            "snapshot_and_terminate",
            "yes",
            "yes",
            NAV_BACK,
        ]
    )
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    match = re.search(r"Snapshot '(snap-[0-9a-f]+)' created\.", output)
    assert match is not None, output
    snapshots = ctx.client_factory.ec2().describe_snapshots(SnapshotIds=[match.group(1)])[
        "Snapshots"
    ]
    assert len(snapshots) == 1


@mock_aws
def test_stale_stopped_wake_up_starts_the_instance() -> None:
    ctx = _app_ctx()
    instance_id = _launch_raw_instance(ctx)
    _stop_instance(ctx, instance_id)

    prompter = FakePrompter(
        ["ec2_audit", "stale_stopped", instance_id, "wake_up_instance", NAV_BACK]
    )
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    state = ctx.client_factory.ec2().describe_instances(InstanceIds=[instance_id])[
        "Reservations"
    ][0]["Instances"][0]["State"]["Name"]
    assert state == "running"


# -- S3 Storage & Lifecycle Audit: menu shape -----------------------------------------


def test_s3_insights_choices_match_the_exact_specified_strings() -> None:
    ctx = _app_ctx()
    choices = AuditFlow(ctx, FakePrompter())._s3_insights_choices()
    labels = [c.title if isinstance(c, Choice) else c.line for c in choices]
    assert labels == [
        "🔓 Public Access & Security Governance",
        "♻️ Missing Lifecycle Policies",
        "💰 Stale Objects in Standard Storage (FinOps)",
        "─" * 66,
        "↩️  Back to Audit Menu",
    ]


# -- S3: Public Access & Security Governance -------------------------------------------


@mock_aws
def test_s3_public_access_governance_flags_an_exposed_bucket() -> None:
    ctx = _capture_console(_app_ctx())
    _create_bucket(ctx, "exposed-bucket")

    prompter = FakePrompter(["s3_audit", "s3_public_access", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 1 exposed buckets missing Public Access Block out of 1." in output
    assert "exposed-bucket" in output


@mock_aws
def test_s3_public_access_governance_excludes_a_fully_blocked_bucket() -> None:
    ctx = _capture_console(_app_ctx())
    _create_bucket(ctx, "safe-bucket")
    ctx.client_factory.s3().put_public_access_block(
        Bucket="safe-bucket",
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    prompter = FakePrompter(["s3_audit", "s3_public_access", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 0 exposed buckets missing Public Access Block out of 1." in output
    assert "safe-bucket" not in output


# -- S3: Missing Lifecycle Policies ------------------------------------------------------


@mock_aws
def test_s3_missing_lifecycle_policies_flags_a_bucket_without_rules() -> None:
    ctx = _capture_console(_app_ctx())
    _create_bucket(ctx, "no-lifecycle-bucket")

    prompter = FakePrompter(["s3_audit", "s3_lifecycle_policies", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 1 buckets without Lifecycle Policies out of 1." in output
    assert "no-lifecycle-bucket" in output


@mock_aws
def test_s3_missing_lifecycle_policies_excludes_a_bucket_with_a_rule() -> None:
    ctx = _capture_console(_app_ctx())
    _create_bucket(ctx, "managed-bucket")
    ctx.client_factory.s3().put_bucket_lifecycle_configuration(
        Bucket="managed-bucket",
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-old",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Expiration": {"Days": 365},
                }
            ]
        },
    )

    prompter = FakePrompter(["s3_audit", "s3_lifecycle_policies", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 0 buckets without Lifecycle Policies out of 1." in output


# -- S3: Stale Objects in Standard Storage (FinOps) ---------------------------------------


@mock_aws
def test_s3_stale_objects_flags_an_object_older_than_90_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """moto stamps ``LastModified`` as real wall-clock time -- ``now`` is shifted forward
    instead, to simulate the object having aged past the 90-day threshold."""
    ctx = _capture_console(_app_ctx())
    _create_bucket(ctx, "stale-bucket")
    ctx.client_factory.s3().put_object(Bucket="stale-bucket", Key="old.txt", Body=b"x" * 2048)

    class _FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=100)

    monkeypatch.setattr(
        "aws_admin_cli.presentation.tui.flows.audit_flow.datetime", _FutureDatetime
    )

    prompter = FakePrompter(["s3_audit", "s3_stale_objects", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 1 stale objects across 1 buckets wasting STANDARD storage." in output
    assert "old.txt" in output


@mock_aws
def test_s3_stale_objects_excludes_a_fresh_object() -> None:
    ctx = _capture_console(_app_ctx())
    _create_bucket(ctx, "fresh-bucket")
    ctx.client_factory.s3().put_object(Bucket="fresh-bucket", Key="new.txt", Body=b"x")

    prompter = FakePrompter(["s3_audit", "s3_stale_objects", NAV_BACK])
    action = AuditFlow(ctx, prompter).menu()

    assert action is NavAction.STAY
    output = ctx.console.file.getvalue()  # type: ignore[attr-defined]
    assert "Found 0 stale objects across 0 buckets wasting STANDARD storage." in output

"""``AuditFlow``: the "Security & Compliance Audit" TUI screen.

One place an admin goes for "what's wrong" reporting across IAM, EC2, and
S3 -- separate from each service's own screen, which stays focused on
create/manage/delete duties (see ``iam_flow.py``/``ec2_flow.py``/
``s3_flow.py``'s own module docstrings).

Split out of the old combined "CloudWatch & Security Audit" screen: the
CloudWatch-specific views (Alarms & Monitoring Governance, Idle Compute
Instances, and the Log Diagnostics placeholder) moved to
``cloudwatch_flow.py`` -- its own top-level "CloudWatch Observability & Logs"
main menu entry. This module keeps the Security/Compliance half. The
underlying logic of every audit below is unchanged from the original
module -- only relocated and re-wired to its own top-level main menu entry.

IAM, EC2, and S3 are all NEW audits shipped directly in this module (never
delegating into ``iam_flow.py``/``ec2_flow.py``/``s3_flow.py``):

- IAM is driven by AWS's own IAM Credential Report
  (``GetCredentialReportUseCase``) -- the same authoritative signal the IAM
  console's own Security Status page is built from -- rather than the
  local-ledger-driven "Audit Inactive Users" ``iam_flow.py`` originally
  shipped (removed from there entirely; see ``iam_audit_metadata`` for what
  it used to read). ``iam_flow.py`` keeps only user creation, policy
  binding, and standard lifecycle management.
- EC2 Resource Hygiene Audit (stale stopped instances) is driven by real EC2
  state via ``build_ec2_use_cases`` -- never ``ec2_flow.py``'s own separate
  "Resource Audit" submenu, which is untouched and still reachable from
  EC2's own screen. Every EC2 remediation action here
  (terminate/snapshot/start) goes through the exact same ``Ec2UseCases``
  guard rails (the managed-tag check, the state-machine validation) as
  ``ec2_flow.py``'s own actions -- this module adds no shortcut around them.
- S3 Storage & Lifecycle Audit is driven by real bucket/object state
  (``GetPublicAccessBlock``, ``GetBucketLifecycleConfiguration``,
  ``ListObjectsV2``) via ``build_s3_use_cases`` -- never ``s3_flow.py``'s own
  separate "Audit" menu entry (its Security & Compliance dashboard), which is
  untouched and still reachable from S3's own screen. This view is read-only
  reporting only, no remediation actions -- unlike EC2's audit, nothing here
  mutates a bucket or its objects.

Per this project's Separation-of-Duties convention: a service screen
manages, this screen reports (and, for EC2, additionally offers the narrow
remediation actions the report itself surfaces a need for).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Self

from rich.table import Table

from aws_admin_cli.application.dto.ec2 import ListInstancesRequest
from aws_admin_cli.application.dto.s3 import ListObjectsRequest
from aws_admin_cli.application.use_cases.iam.get_credential_report import CredentialReportEntry
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.domain.models.s3 import Bucket, StorageClass
from aws_admin_cli.presentation.tui.flows._shared import (
    announce_result,
    clear_and_banner,
    confirm_destructive,
    confirm_yes_no,
    run_with_spinner,
)
from aws_admin_cli.presentation.tui.flows.error_handler import aws_error_handler
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice, Separator
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter
from aws_admin_cli.presentation.wiring import (
    build_ec2_use_cases,
    build_iam_use_cases,
    build_s3_use_cases,
)

__all__ = ["AuditFlow"]

_IAM_AUDIT = "iam_audit"
_EC2_AUDIT = "ec2_audit"
_S3_AUDIT = "s3_audit"

_INACTIVE_USERS = "inactive_users"
_DISABLED_USERS = "disabled_users"
_PASSWORD_UNCHANGED = "password_unchanged"
_NO_MFA = "no_mfa"

# Two severities of the same "days since last console/API activity" signal --
# Inactive is the early warning, Disabled is the "should already have been
# disabled" tier -- both computed straight off the Credential Report, no
# extra AWS calls needed.
_INACTIVE_THRESHOLD_DAYS = 15
_DISABLED_THRESHOLD_DAYS = 30
_PASSWORD_THRESHOLD_DAYS = 90

_RETURN_PROMPT = "Press Enter to return to IAM Security Insights..."

# -- EC2 Resource Hygiene Audit --------------------------------------------------------

_STALE_STOPPED = "stale_stopped"
_SCHEDULERS_PENDING = "schedulers_pending"
_PENDING_REASON = "Coming soon"

_STALE_STOPPED_THRESHOLD_DAYS = 30

_TERMINATE_INSTANCE = "terminate_instance"
_SNAPSHOT_AND_TERMINATE = "snapshot_and_terminate"
_WAKE_UP_INSTANCE = "wake_up_instance"

# -- S3 Storage & Lifecycle Audit --------------------------------------------------

_S3_PUBLIC_ACCESS = "s3_public_access"
_S3_LIFECYCLE_POLICIES = "s3_lifecycle_policies"
_S3_STALE_OBJECTS = "s3_stale_objects"

_STALE_OBJECT_AGE_DAYS = 90

_S3_RETURN_PROMPT = "Press Enter to return to S3 Audit..."


def _fmt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value is not None else "Never"


def _days_since(value: datetime | None, *, now: datetime) -> int | None:
    return (now - value).days if value is not None else None


def _inactivity_matches(
    entries: list[CredentialReportEntry], *, now: datetime, threshold_days: int
) -> list[CredentialReportEntry]:
    """Users whose ``last_activity`` is more than ``threshold_days`` old.

    Never-active (``last_activity is None``) always matches, regardless of
    threshold -- an account nobody has ever logged into or used a key for is
    at least as stale as one merely idle past the cutoff.
    """
    return [
        e
        for e in entries
        if (days := _days_since(e.last_activity, now=now)) is None or days > threshold_days
    ]


def _password_unchanged_matches(
    entries: list[CredentialReportEntry], *, now: datetime, threshold_days: int
) -> list[CredentialReportEntry]:
    """Users with a console password older than ``threshold_days``.

    A user with no password at all (``password_last_changed is None``) is
    excluded -- there is no password to have gone stale.
    """
    return [
        e
        for e in entries
        if e.password_last_changed is not None
        and (now - e.password_last_changed).days > threshold_days
    ]


def _no_mfa_matches(entries: list[CredentialReportEntry]) -> list[CredentialReportEntry]:
    """Users with no active MFA device."""
    return [e for e in entries if not e.mfa_active]


@dataclass(slots=True)
class AuditFlow:
    """Root screen for cross-service Security & Compliance audits."""

    title: ClassVar[str] = "🛡️ Security & Compliance Audit"

    ctx: AppContext
    prompter: Prompter

    @aws_error_handler
    def menu(self: Self) -> NavAction:
        """Show the audit root menu once."""
        clear_and_banner(self.ctx)
        selected = self.prompter.select(
            "Security & Compliance Audit -- choose a domain:", self._choices()
        )
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected == NAV_BACK:
            return NavAction.BACK
        if selected == _IAM_AUDIT:
            self._iam_security_insights()
        elif selected == _EC2_AUDIT:
            self._ec2_resource_hygiene_audit()
        elif selected == _S3_AUDIT:
            self._s3_storage_lifecycle_audit()
        return NavAction.STAY

    def _choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(title="🔐 IAM Identity & Access Audit", value=_IAM_AUDIT),
            Choice(title="🖥️  EC2 Resource Hygiene Audit", value=_EC2_AUDIT),
            Choice(title="📦 S3 Storage & Lifecycle Audit", value=_S3_AUDIT),
            Separator(),
            Choice(title="↩️  Back to Main Menu", value=NAV_BACK),
        ]

    # -- IAM: Credential-Report-driven Security Insights --------------------------

    def _credential_report(self: Self) -> list[CredentialReportEntry]:
        return build_iam_use_cases(self.ctx).get_credential_report.execute()

    def _security_insights_choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(
                title=f"👤 Inactive users (> {_INACTIVE_THRESHOLD_DAYS} days)",
                value=_INACTIVE_USERS,
            ),
            Choice(
                title=f"🛑 Disabled users (> {_DISABLED_THRESHOLD_DAYS} days)",
                value=_DISABLED_USERS,
            ),
            Choice(
                title=f"🔑 Password unchanged (> {_PASSWORD_THRESHOLD_DAYS} days)",
                value=_PASSWORD_UNCHANGED,
            ),
            Choice(title="🛡️ Users without MFA active", value=_NO_MFA),
            Separator(),
            Choice(title="↩️  Back to Audit Menu", value=NAV_BACK),
        ]

    def _iam_security_insights(self: Self) -> None:
        while True:
            clear_and_banner(self.ctx)
            selected = self.prompter.select(
                "IAM Security Insights -- choose a view:", self._security_insights_choices()
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _INACTIVE_USERS:
                self._view_inactive_users()
            elif selected == _DISABLED_USERS:
                self._view_disabled_users()
            elif selected == _PASSWORD_UNCHANGED:
                self._view_password_unchanged()
            elif selected == _NO_MFA:
                self._view_no_mfa()

    def _render_finding(
        self: Self,
        *,
        finding_label: str,
        count: int,
        total: int,
        table: Table,
    ) -> None:
        """Header (finding vs. total pool) then the table, on a freshly cleared screen."""
        clear_and_banner(self.ctx)
        self.ctx.console.print(
            f"[bold yellow]⚠️ Found {count} {finding_label} out of {total} "
            "total IAM accounts analyzed.[/]"
        )
        self.ctx.console.print(table)
        self.prompter.pause(_RETURN_PROMPT)

    def _view_inactive_users(self: Self) -> None:
        entries = self._credential_report()
        now = datetime.now(UTC)
        matches = _inactivity_matches(entries, now=now, threshold_days=_INACTIVE_THRESHOLD_DAYS)
        table = Table(title=f"Inactive Users (> {_INACTIVE_THRESHOLD_DAYS} days)")
        table.add_column("Username", style="bold")
        table.add_column("Last Activity")
        table.add_column("Days Inactive", justify="right")
        for entry in matches:
            days = _days_since(entry.last_activity, now=now)
            table.add_row(
                entry.user_name,
                _fmt(entry.last_activity),
                str(days) if days is not None else "Never active",
            )
        self._render_finding(
            finding_label="inactive users", count=len(matches), total=len(entries), table=table
        )

    def _view_disabled_users(self: Self) -> None:
        entries = self._credential_report()
        now = datetime.now(UTC)
        matches = _inactivity_matches(entries, now=now, threshold_days=_DISABLED_THRESHOLD_DAYS)
        table = Table(title=f"Disabled-Worthy Users (> {_DISABLED_THRESHOLD_DAYS} days)")
        table.add_column("Username", style="bold")
        table.add_column("Last Activity")
        table.add_column("Days Inactive", justify="right")
        for entry in matches:
            days = _days_since(entry.last_activity, now=now)
            table.add_row(
                entry.user_name,
                _fmt(entry.last_activity),
                str(days) if days is not None else "Never active",
            )
        self._render_finding(
            finding_label="disabled users", count=len(matches), total=len(entries), table=table
        )

    def _view_password_unchanged(self: Self) -> None:
        entries = self._credential_report()
        now = datetime.now(UTC)
        matches = _password_unchanged_matches(
            entries, now=now, threshold_days=_PASSWORD_THRESHOLD_DAYS
        )
        table = Table(title=f"Password Unchanged (> {_PASSWORD_THRESHOLD_DAYS} days)")
        table.add_column("Username", style="bold")
        table.add_column("Password Last Changed")
        table.add_column("Days Since Change", justify="right")
        for entry in matches:
            days = (now - entry.password_last_changed).days if entry.password_last_changed else 0
            table.add_row(entry.user_name, _fmt(entry.password_last_changed), str(days))
        self._render_finding(
            finding_label="users with password unchanged",
            count=len(matches),
            total=len(entries),
            table=table,
        )

    def _view_no_mfa(self: Self) -> None:
        entries = self._credential_report()
        matches = _no_mfa_matches(entries)
        table = Table(title="Users Without MFA Active")
        table.add_column("Username", style="bold")
        table.add_column("Console Access")
        table.add_column("Active Access Keys")
        for entry in matches:
            active_keys = sum(
                1 for active in (entry.access_key_1_active, entry.access_key_2_active) if active
            )
            table.add_row(
                entry.user_name,
                "Yes" if entry.password_enabled else "No",
                str(active_keys),
            )
        self._render_finding(
            finding_label="users without MFA active",
            count=len(matches),
            total=len(entries),
            table=table,
        )

    # -- EC2: Resource Hygiene Audit -------------------------------------------------

    def _ec2_insights_choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(
                title=f"🛑 Stale Stopped Instances (> {_STALE_STOPPED_THRESHOLD_DAYS} days)",
                value=_STALE_STOPPED,
            ),
            Choice(
                title="⚡ Automated Start/Stop Schedulers (FinOps) [Pending]",
                value=_SCHEDULERS_PENDING,
                disabled=_PENDING_REASON,
            ),
            Separator(),
            Choice(title="↩️  Back to Audit Menu", value=NAV_BACK),
        ]

    def _ec2_resource_hygiene_audit(self: Self) -> None:
        """EC2 Resource Hygiene Audit root: the loop hosting every EC2 sub-view."""
        while True:
            clear_and_banner(self.ctx)
            selected = self.prompter.select(
                "EC2 Resource Hygiene Audit -- choose a view:",
                self._ec2_insights_choices(),
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _STALE_STOPPED:
                self._stale_stopped_instances()
            # _SCHEDULERS_PENDING is a `disabled=` choice -- never selectable, so
            # there is nothing to dispatch to for it.

    @staticmethod
    def _instance_table(title: str, instances: list[Instance]) -> Table:
        """The ``Instance ID | Name | Type | State`` table shape every EC2 view here shares."""
        table = Table(title=title)
        table.add_column("Instance ID", style="bold")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("State")
        for instance in instances:
            table.add_row(
                instance.instance_id,
                instance.display_name,
                instance.instance_type,
                instance.state.value,
            )
        return table

    @staticmethod
    def _instance_picker_choices(instances: list[Instance]) -> list[Choice | Separator]:
        choices: list[Choice | Separator] = [
            Choice(title=f"{instance.instance_id} ({instance.display_name})",
                   value=instance.instance_id)
            for instance in instances
        ]
        choices.append(Separator())
        choices.append(Choice(title="↩️  Back", value=NAV_BACK))
        return choices

    # -- EC2: Stale Stopped Instances (> 30 days) -----------------------------------

    def _stale_stopped_instances(self: Self) -> None:
        """Stopped instances -> pick one -> remediate, redrawn fresh after every action.

        Per this feature's LocalStack accommodation, ">30 days" is simulated
        by simply listing every currently-stopped instance -- LocalStack
        carries no real multi-week stop-duration history to check against.
        """
        while True:
            clear_and_banner(self.ctx)
            instances = build_ec2_use_cases(self.ctx).list_instances.execute(
                ListInstancesRequest(state="stopped")
            )
            self.ctx.console.print(
                f"[bold red]🗑️ Found {len(instances)} stopped instances inactive for > "
                f"{_STALE_STOPPED_THRESHOLD_DAYS} days. Releasing orphaned EBS storage "
                "recommended.[/]"
            )
            self.ctx.console.print(
                self._instance_table(
                    f"Stale Stopped Instances (> {_STALE_STOPPED_THRESHOLD_DAYS} days)", instances
                )
            )
            if not instances:
                self.prompter.pause()
                return

            selected = self.prompter.select(
                "Select an instance for remediation:", self._instance_picker_choices(instances)
            )
            if selected is None or selected == NAV_BACK:
                return
            instance = next(i for i in instances if i.instance_id == selected)
            self._stale_stopped_instance_actions(instance)

    def _stale_stopped_instance_actions(self: Self, instance: Instance) -> None:
        selected = self.prompter.select(
            f"Instance '{instance.instance_id}' -- choose a remediation action:",
            [
                Choice(
                    title="🗑️ Terminate Instance (Clean up dead weight)",
                    value=_TERMINATE_INSTANCE,
                ),
                Choice(
                    title="📦 Snapshot & Terminate (Safe Cleanup)", value=_SNAPSHOT_AND_TERMINATE
                ),
                Choice(title="🚀 Wake Up / Start Instance", value=_WAKE_UP_INSTANCE),
                Separator(),
                Choice(title="↩️  Back to EC2 Insights", value=NAV_BACK),
            ],
        )
        if selected is None or selected == NAV_BACK:
            return
        if selected == _TERMINATE_INSTANCE:
            self._terminate_stale_instance(instance)
        elif selected == _SNAPSHOT_AND_TERMINATE:
            self._snapshot_and_terminate_stale_instance(instance)
        elif selected == _WAKE_UP_INSTANCE:
            self._start_stale_instance(instance)

    def _terminate_stale_instance(self: Self, instance: Instance) -> None:
        """Terminate ``instance`` -- same force-retry contract ``ec2_flow.py``'s own delete uses.

        A stopped instance this audit surfaced may well not carry the
        ``ManagedBy=aws-admin-cli`` tag (it could be anything sitting idle in
        the account), so ``TerminateInstanceUseCase`` refuses it exactly as
        it would from ``ec2_flow.py`` unless the admin explicitly confirms
        ``--force`` here too -- no shortcut around that guard rail.
        """
        if not confirm_destructive(self.prompter, kind="instance", name=instance.instance_id):
            return
        use_cases = build_ec2_use_cases(self.ctx)
        try:
            result = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Terminating instance '{instance.instance_id}'...[/bold green]",
                lambda: use_cases.terminate_instance.execute(
                    instance, force=False, dry_run=False, wait=False, timeout_s=300
                ),
            )
        except ValidationError as exc:
            self.ctx.err_console.print(f"[red]{exc}[/]")
            if exc.hint:
                self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
            if not confirm_yes_no(
                self.prompter,
                "This instance is not managed by this CLI. Terminate it anyway (--force)?",
            ):
                return
            result = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Terminating instance '{instance.instance_id}'...[/bold green]",
                lambda: use_cases.terminate_instance.execute(
                    instance, force=True, dry_run=False, wait=False, timeout_s=300
                ),
            )
        label = result.display_name if result is not None else instance.display_name
        announce_result(self.ctx, self.prompter, f"[green]Termination requested for '{label}'.[/]")

    def _snapshot_and_terminate_stale_instance(self: Self, instance: Instance) -> None:
        """Snapshot the root EBS volume (if any), then run the exact same terminate flow."""
        volume_id = instance.root_volume_id
        if volume_id is None:
            self.ctx.err_console.print(
                "[yellow]No EBS volume found on this instance -- nothing to snapshot.[/]"
            )
        else:
            use_cases = build_ec2_use_cases(self.ctx)
            snapshot_id = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Snapshotting volume '{volume_id}'...[/bold green]",
                lambda: use_cases.create_snapshot.execute(
                    volume_id,
                    f"aws-admin-cli pre-termination snapshot of {instance.instance_id}",
                ),
            )
            self.ctx.console.print(f"[green]Snapshot '{snapshot_id}' created.[/]")
        self._terminate_stale_instance(instance)

    def _start_stale_instance(self: Self, instance: Instance) -> None:
        use_cases = build_ec2_use_cases(self.ctx)
        try:
            result = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Starting instance '{instance.instance_id}'...[/bold green]",
                lambda: use_cases.start_instance.execute(instance, wait=False, timeout_s=300),
            )
        except ValidationError as exc:
            self.ctx.err_console.print(f"[red]{exc}[/]")
            if exc.hint:
                self.ctx.err_console.print(f"[yellow]{exc.hint}[/]")
            self.prompter.pause()
            return
        announce_result(
            self.ctx, self.prompter, f"[green]Instance '{result.display_name}' is starting.[/]"
        )

    # -- S3: Storage & Lifecycle Audit ------------------------------------------------

    def _s3_insights_choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(title="🔓 Public Access & Security Governance", value=_S3_PUBLIC_ACCESS),
            Choice(title="♻️ Missing Lifecycle Policies", value=_S3_LIFECYCLE_POLICIES),
            Choice(
                title="💰 Stale Objects in Standard Storage (FinOps)", value=_S3_STALE_OBJECTS
            ),
            Separator(),
            Choice(title="↩️  Back to Audit Menu", value=NAV_BACK),
        ]

    def _s3_storage_lifecycle_audit(self: Self) -> None:
        """S3 Storage & Lifecycle Audit root: the loop hosting every S3 sub-view."""
        while True:
            clear_and_banner(self.ctx)
            selected = self.prompter.select(
                "S3 Storage & Lifecycle Audit -- choose a view:", self._s3_insights_choices()
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _S3_PUBLIC_ACCESS:
                self._s3_public_access_governance()
            elif selected == _S3_LIFECYCLE_POLICIES:
                self._s3_missing_lifecycle_policies()
            elif selected == _S3_STALE_OBJECTS:
                self._s3_stale_objects_standard_storage()

    def _s3_public_access_governance(self: Self) -> None:
        """Buckets missing (or not fully enforcing) Block Public Access.

        Reuses ``AuditBucketsUseCase`` (the same engine S3's own Security &
        Compliance dashboard is built from) for the public-access read, so
        this view can never disagree with what ``get_public_access_block``
        itself reports.
        """
        clear_and_banner(self.ctx)
        use_cases = build_s3_use_cases(self.ctx)
        entries = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Scanning bucket public access configuration...[/bold green]",
            lambda: use_cases.audit_buckets.execute(),
        )
        exposed = [e for e in entries if not e.public_access_blocked]
        regions = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Resolving bucket regions...[/bold green]",
            lambda: {e.name: use_cases.gateway.get_bucket_location(e.name) for e in exposed},
        )

        table = Table(title="Exposed Buckets (Missing Public Access Block)")
        table.add_column("Bucket Name", style="bold")
        table.add_column("Region")
        table.add_column("Public Access Status")
        for entry in exposed:
            table.add_row(entry.name, regions[entry.name], "[bold red]PUBLIC ALERT[/]")

        clear_and_banner(self.ctx)
        self.ctx.console.print(
            f"[bold red]⚠️ Found {len(exposed)} exposed buckets missing Public Access Block "
            f"out of {len(entries)}.[/]"
        )
        self.ctx.console.print(table)
        self.prompter.pause(_S3_RETURN_PROMPT)

    def _s3_missing_lifecycle_policies(self: Self) -> None:
        """Buckets with no lifecycle rule at all (no retention/expiration configured)."""
        clear_and_banner(self.ctx)
        use_cases = build_s3_use_cases(self.ctx)
        buckets = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Listing buckets...[/bold green]",
            lambda: use_cases.list_buckets.execute(),
        )
        missing = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Checking lifecycle policies...[/bold green]",
            lambda: [b for b in buckets if not use_cases.gateway.has_lifecycle_policy(b.name)],
        )

        table = Table(title="Buckets Without Lifecycle Policies")
        table.add_column("Bucket Name", style="bold")
        table.add_column("Region")
        for bucket in missing:
            table.add_row(bucket.name, use_cases.gateway.get_bucket_location(bucket.name))

        clear_and_banner(self.ctx)
        self.ctx.console.print(
            f"[bold yellow]⚠️ Found {len(missing)} buckets without Lifecycle Policies "
            f"out of {len(buckets)}.[/]"
        )
        self.ctx.console.print(table)
        self.prompter.pause(_S3_RETURN_PROMPT)

    def _collect_stale_objects(
        self: Self, buckets: list[Bucket]
    ) -> list[tuple[str, str, int, int]]:
        """``(bucket_name, key, size_bytes, age_days)`` for every stale STANDARD object.

        "Stale" means STANDARD storage class and older than
        ``_STALE_OBJECT_AGE_DAYS`` -- every other storage class is already a
        deliberate cost-optimization choice, not something FinOps needs
        flagged.
        """
        use_cases = build_s3_use_cases(self.ctx)
        now = datetime.now(UTC)
        stale: list[tuple[str, str, int, int]] = []
        for bucket in buckets:
            listing = use_cases.list_objects.execute(ListObjectsRequest(bucket=bucket.name))
            for obj in listing.objects:
                if obj.storage_class is not StorageClass.STANDARD:
                    continue
                age_days = (now - obj.last_modified).days
                if age_days > _STALE_OBJECT_AGE_DAYS:
                    stale.append((bucket.name, obj.key, obj.size, age_days))
        return stale

    def _s3_stale_objects_standard_storage(self: Self) -> None:
        """STANDARD-class objects older than 90 days, across every bucket (FinOps)."""
        clear_and_banner(self.ctx)
        use_cases = build_s3_use_cases(self.ctx)
        buckets = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Listing buckets...[/bold green]",
            lambda: use_cases.list_buckets.execute(),
        )
        stale = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Scanning objects for stale STANDARD storage...[/bold green]",
            lambda: self._collect_stale_objects(buckets),
        )

        table = Table(title=f"Stale Objects in STANDARD Storage (> {_STALE_OBJECT_AGE_DAYS} days)")
        table.add_column("Bucket Name", style="bold")
        table.add_column("Object Key")
        table.add_column("Size (MB)", justify="right")
        table.add_column("Age (Days)", justify="right")
        for bucket_name, key, size_bytes, age_days in stale:
            table.add_row(bucket_name, key, f"{size_bytes / (1024 * 1024):.2f}", str(age_days))

        bucket_count = len({bucket_name for bucket_name, _, _, _ in stale})
        clear_and_banner(self.ctx)
        self.ctx.console.print(
            f"[bold yellow]💰 Found {len(stale)} stale objects across {bucket_count} buckets "
            "wasting STANDARD storage.[/]"
        )
        self.ctx.console.print(table)
        self.prompter.pause(_S3_RETURN_PROMPT)

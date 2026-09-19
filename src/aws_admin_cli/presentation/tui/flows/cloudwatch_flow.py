"""``CloudWatchFlow``: the "CloudWatch Observability & Logs" TUI screen.

Split out of the old combined "CloudWatch & Security Audit" screen (now
``audit_flow.py``, Security & Compliance only) -- this module keeps exactly
the observability-facing half: Alarms & Monitoring Governance and Idle
Compute Instances, both driven by real CloudWatch state
(``DescribeAlarms``, ``CPUUtilization``) via ``build_cloudwatch_use_cases``,
plus a placeholder for future Log Group/log-tailing functionality. The
underlying logic is unchanged from the original module -- only relocated and
re-wired to its own top-level main menu entry.

Every EC2 alarm-attachment action here goes through the exact same
``CloudWatchUseCases`` guard rails as everywhere else in this project -- this
module adds no shortcut around them. Per this project's Separation-of-Duties
convention: a service screen (``ec2_flow.py``) manages, this screen reports
(and, narrowly, lets an admin attach the alarm a report just showed is
missing).
"""

from dataclasses import dataclass
from typing import ClassVar, Self

from rich.table import Table

from aws_admin_cli.application.dto.ec2 import ListInstancesRequest
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.presentation.tui.flows._shared import (
    announce_result,
    clear_and_banner,
    run_with_spinner,
)
from aws_admin_cli.presentation.tui.flows.error_handler import aws_error_handler
from aws_admin_cli.presentation.tui.menu import NAV_BACK, NAV_EXIT, Choice, Separator
from aws_admin_cli.presentation.tui.navigation import NavAction
from aws_admin_cli.presentation.tui.prompter import Prompter
from aws_admin_cli.presentation.wiring import build_cloudwatch_use_cases, build_ec2_use_cases

__all__ = ["CloudWatchFlow"]

_ALARMS_GOVERNANCE = "alarms_governance"
_IDLE_COMPUTE = "idle_compute"
_LIVE_METRICS_PENDING = "live_metrics_pending"
_PENDING_REASON = "Coming soon"

_IDLE_LOOKBACK_DAYS = 15
_IDLE_CPU_THRESHOLD_PERCENT = 5.0

_MONITORED_INSTANCES = "monitored_instances"
_UNMONITORED_INSTANCES = "unmonitored_instances"

_ATTACH_CPU_ALARM = "attach_cpu_alarm"
_ATTACH_STATUS_CHECK_ALARM = "attach_status_check_alarm"


@dataclass(slots=True)
class CloudWatchFlow:
    """Root screen for CloudWatch Observability: Alarms Governance, Idle Compute, Logs."""

    title: ClassVar[str] = "📈 CloudWatch Observability & Logs"

    ctx: AppContext
    prompter: Prompter

    @aws_error_handler
    def menu(self: Self) -> NavAction:
        """Show the CloudWatch root menu once."""
        clear_and_banner(self.ctx)
        selected = self.prompter.select(
            "CloudWatch Observability & Logs -- choose a view:", self._choices()
        )
        if selected is None or selected == NAV_EXIT:
            return NavAction.EXIT
        if selected == NAV_BACK:
            return NavAction.BACK
        if selected == _ALARMS_GOVERNANCE:
            self._alarms_monitoring_governance()
        elif selected == _IDLE_COMPUTE:
            self._idle_compute_instances()
        # _LIVE_METRICS_PENDING is a `disabled=` choice -- never selectable, so
        # there is nothing to dispatch to for it.
        return NavAction.STAY

    def _choices(self: Self) -> list[Choice | Separator]:
        return [
            Choice(title="🛡️ Alarms & Monitoring Governance", value=_ALARMS_GOVERNANCE),
            Choice(
                title=f"💤 Idle Compute Instances (Low CPU > {_IDLE_LOOKBACK_DAYS} days)",
                value=_IDLE_COMPUTE,
            ),
            Choice(
                title="📈 Live Metrics & Log Diagnostics [Pending]",
                value=_LIVE_METRICS_PENDING,
                disabled=_PENDING_REASON,
            ),
            Separator(),
            Choice(title="↩️  Back to Main Menu", value=NAV_BACK),
        ]

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

    # -- Alarms & Monitoring Governance -----------------------------------------

    def _monitored_instance_ids(self: Self) -> set[str]:
        alarms = build_cloudwatch_use_cases(self.ctx).list_alarms.execute()
        ids = (alarm.dimension_value("InstanceId") for alarm in alarms)
        return {instance_id for instance_id in ids if instance_id is not None}

    def _alarms_monitoring_governance(self: Self) -> None:
        while True:
            clear_and_banner(self.ctx)
            selected = self.prompter.select(
                "Alarms & Monitoring Governance -- choose a view:",
                [
                    Choice(
                        title="✅ Monitored Instances (With active alarms)",
                        value=_MONITORED_INSTANCES,
                    ),
                    Choice(
                        title="⚠️ Unmonitored Instances (Missing alarms)",
                        value=_UNMONITORED_INSTANCES,
                    ),
                    Separator(),
                    Choice(title="↩️  Back to CloudWatch Menu", value=NAV_BACK),
                ],
            )
            if selected is None or selected == NAV_BACK:
                return
            if selected == _MONITORED_INSTANCES:
                self._view_monitored_instances()
            elif selected == _UNMONITORED_INSTANCES:
                self._unmonitored_instances_loop()

    def _view_monitored_instances(self: Self) -> None:
        instances = build_ec2_use_cases(self.ctx).list_instances.execute(
            ListInstancesRequest(state="running")
        )
        monitored_ids = self._monitored_instance_ids()
        matches = [i for i in instances if i.instance_id in monitored_ids]
        clear_and_banner(self.ctx)
        self.ctx.console.print(
            f"[bold green]✅ {len(matches)} of {len(instances)} active instances have "
            "CloudWatch alarms.[/]"
        )
        self.ctx.console.print(self._instance_table("Monitored Instances", matches))
        self.prompter.pause()

    def _unmonitored_instances_loop(self: Self) -> None:
        """Unmonitored instances -> pick one -> attach an alarm, redrawn after every action."""
        while True:
            instances = build_ec2_use_cases(self.ctx).list_instances.execute(
                ListInstancesRequest(state="running")
            )
            monitored_ids = self._monitored_instance_ids()
            unmonitored = [i for i in instances if i.instance_id not in monitored_ids]

            clear_and_banner(self.ctx)
            self.ctx.console.print(
                f"[bold yellow]⚠️ Found {len(unmonitored)} active instances without "
                "CloudWatch Alarms.[/]"
            )
            self.ctx.console.print(self._instance_table("Unmonitored Instances", unmonitored))
            if not unmonitored:
                self.prompter.pause()
                return

            selected = self.prompter.select(
                "Select an instance to protect:", self._instance_picker_choices(unmonitored)
            )
            if selected is None or selected == NAV_BACK:
                return
            instance = next(i for i in unmonitored if i.instance_id == selected)
            self._attach_alarm_actions(instance)

    def _attach_alarm_actions(self: Self, instance: Instance) -> None:
        selected = self.prompter.select(
            f"Instance '{instance.instance_id}' -- attach an alarm:",
            [
                Choice(title="➕ Attach CPU Alarm (> 80% utilization)", value=_ATTACH_CPU_ALARM),  # noqa: RUF001
                Choice(title="➕ Attach Status Check Alarm", value=_ATTACH_STATUS_CHECK_ALARM),  # noqa: RUF001
                Separator(),
                Choice(title="↩️  Back", value=NAV_BACK),
            ],
        )
        if selected is None or selected == NAV_BACK:
            return
        use_cases = build_cloudwatch_use_cases(self.ctx)
        if selected == _ATTACH_CPU_ALARM:
            alarm_name = run_with_spinner(
                self.ctx.err_console,
                f"[bold green]Attaching CPU alarm to '{instance.instance_id}'...[/bold green]",
                lambda: use_cases.create_cpu_alarm.execute(instance.instance_id),
            )
        else:
            alarm_name = run_with_spinner(
                self.ctx.err_console,
                "[bold green]Attaching status check alarm to "
                f"'{instance.instance_id}'...[/bold green]",
                lambda: use_cases.create_status_check_alarm.execute(instance.instance_id),
            )
        announce_result(self.ctx, self.prompter, f"[green]Alarm '{alarm_name}' attached.[/]")

    # -- Idle Compute Instances (Low CPU > 15 days) ------------------------------

    def _collect_cpu_readings(
        self: Self, instances: list[Instance]
    ) -> list[tuple[Instance, float | None]]:
        cpu_use_case = build_cloudwatch_use_cases(self.ctx).get_average_cpu_utilization
        return [
            (instance, cpu_use_case.execute(instance.instance_id, days=_IDLE_LOOKBACK_DAYS))
            for instance in instances
        ]

    def _idle_compute_instances(self: Self) -> None:
        """Running instances whose average CPU (CloudWatch, last N days) reads low or unknown.

        "Unknown" (no CloudWatch datapoints -- the common case on LocalStack,
        which doesn't simulate real metric history) is flagged rather than
        excluded: an idle-compute audit that silently skips whatever it has
        no data for is worse than one that surfaces it plainly for a human
        to check, same principle this app's other "unknown -> flag it"
        audit signals already follow.
        """
        clear_and_banner(self.ctx)
        instances = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Fetching running instances...[/bold green]",
            lambda: build_ec2_use_cases(self.ctx).list_instances.execute(
                ListInstancesRequest(state="running")
            ),
        )
        readings = run_with_spinner(
            self.ctx.err_console,
            "[bold green]Reading CloudWatch metrics for running instances...[/bold green]",
            lambda: self._collect_cpu_readings(instances),
        )
        idle = [
            (instance, cpu)
            for instance, cpu in readings
            if cpu is None or cpu < _IDLE_CPU_THRESHOLD_PERCENT
        ]

        table = Table(title=f"Idle Compute Instances (Low CPU > {_IDLE_LOOKBACK_DAYS} days)")
        table.add_column("Instance ID", style="bold")
        table.add_column("Name")
        table.add_column("Type")
        table.add_column("Avg CPU %", justify="right")
        for instance, cpu in idle:
            table.add_row(
                instance.instance_id,
                instance.display_name,
                instance.instance_type,
                f"{cpu:.1f}%" if cpu is not None else "No data",
            )

        clear_and_banner(self.ctx)
        self.ctx.console.print(
            f"[bold yellow]💤 Found {len(idle)} idle instances (avg CPU < "
            f"{_IDLE_CPU_THRESHOLD_PERCENT:.0f}% over {_IDLE_LOOKBACK_DAYS} days) out of "
            f"{len(instances)} running instances analyzed.[/]"
        )
        self.ctx.console.print(table)
        self.prompter.pause()

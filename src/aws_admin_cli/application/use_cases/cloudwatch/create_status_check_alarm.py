"""Use case: attach a standard StatusCheckFailed alarm to an EC2 instance."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.cloudwatch_gateway import CloudWatchGateway

__all__ = ["CreateStatusCheckAlarmUseCase", "status_check_alarm_name"]

_STATUS_CHECK_THRESHOLD = 1.0
_EVALUATION_PERIODS = 2
_PERIOD_SECONDS = 300


def status_check_alarm_name(instance_id: str) -> str:
    """The deterministic alarm name this app always uses for ``instance_id``'s status alarm.

    Same "update in place, never duplicate" reasoning as ``cpu_alarm_name``.
    """
    return f"aws-admin-cli-status-check-failed-{instance_id}"


@dataclass(frozen=True, slots=True)
class CreateStatusCheckAlarmUseCase:
    """Create a ``StatusCheckFailed`` alarm for one instance -- fires on any failed check."""

    gateway: CloudWatchGateway

    def execute(self: Self, instance_id: str) -> str:
        """Create the alarm; returns its name."""
        alarm_name = status_check_alarm_name(instance_id)
        self.gateway.put_metric_alarm(
            alarm_name=alarm_name,
            metric_name="StatusCheckFailed",
            namespace="AWS/EC2",
            dimensions={"InstanceId": instance_id},
            statistic="Maximum",
            period=_PERIOD_SECONDS,
            evaluation_periods=_EVALUATION_PERIODS,
            threshold=_STATUS_CHECK_THRESHOLD,
            comparison_operator="GreaterThanOrEqualToThreshold",
        )
        return alarm_name

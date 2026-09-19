"""Use case: attach a standard high-CPU-utilization alarm to an EC2 instance."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.cloudwatch_gateway import CloudWatchGateway

__all__ = ["CreateCpuAlarmUseCase", "cpu_alarm_name"]

_CPU_THRESHOLD_PERCENT = 80.0
_EVALUATION_PERIODS = 2
_PERIOD_SECONDS = 300


def cpu_alarm_name(instance_id: str) -> str:
    """The deterministic alarm name this app always uses for ``instance_id``'s CPU alarm.

    Deterministic (not a random suffix) so re-running this use case against
    an instance that already has one updates it in place -- ``PutMetricAlarm``
    is itself an upsert -- rather than accumulating duplicate alarms.
    """
    return f"aws-admin-cli-cpu-high-{instance_id}"


@dataclass(frozen=True, slots=True)
class CreateCpuAlarmUseCase:
    """Create a >80% average CPU utilization alarm for one instance.

    80%/2 evaluation periods of 5 minutes is the same "sustained high CPU"
    default the AWS Console's own instance-alarm shortcut proposes -- not a
    number this app invented.
    """

    gateway: CloudWatchGateway

    def execute(self: Self, instance_id: str) -> str:
        """Create the alarm; returns its name."""
        alarm_name = cpu_alarm_name(instance_id)
        self.gateway.put_metric_alarm(
            alarm_name=alarm_name,
            metric_name="CPUUtilization",
            namespace="AWS/EC2",
            dimensions={"InstanceId": instance_id},
            statistic="Average",
            period=_PERIOD_SECONDS,
            evaluation_periods=_EVALUATION_PERIODS,
            threshold=_CPU_THRESHOLD_PERCENT,
            comparison_operator="GreaterThanThreshold",
        )
        return alarm_name

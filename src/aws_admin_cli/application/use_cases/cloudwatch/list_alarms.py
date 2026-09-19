"""Use case: list every CloudWatch metric alarm in the account/region."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.cloudwatch import MetricAlarm
from aws_admin_cli.domain.ports.cloudwatch_gateway import CloudWatchGateway


@dataclass(frozen=True, slots=True)
class ListAlarmsUseCase:
    """List every metric alarm -- the raw material for cross-referencing against instances."""

    gateway: CloudWatchGateway

    def execute(self: Self) -> list[MetricAlarm]:
        """Return every metric alarm in this region."""
        return self.gateway.describe_alarms()

"""Use case: this instance's average CPU utilization over a recent lookback window."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Self

from aws_admin_cli.domain.ports.cloudwatch_gateway import CloudWatchGateway


@dataclass(frozen=True, slots=True)
class GetAverageCpuUtilizationUseCase:
    """Average ``CPUUtilization`` for one instance over the last ``days`` days."""

    gateway: CloudWatchGateway

    def execute(self: Self, instance_id: str, *, days: int) -> float | None:
        """Return the average, or ``None`` if CloudWatch has no datapoints for the window."""
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        return self.gateway.get_average_cpu_utilization(instance_id, start=start, end=end)

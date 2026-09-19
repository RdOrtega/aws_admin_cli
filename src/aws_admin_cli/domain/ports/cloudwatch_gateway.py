"""The CloudWatchGateway port: alarm inspection/creation, returning domain models only.

Implementations must never leak a raw AWS SDK value across this boundary --
every method returns an ``aws_admin_cli.domain.models.cloudwatch`` model (or
``None``/``list``/``str``/nothing), and raises only
``aws_admin_cli.core.exceptions`` types (via
``infrastructure.aws.error_mapper.aws_error_boundary``), never a raw
client-error exception from the underlying SDK. Scoped to what the EC2
Compute & CloudWatch Insights audit needs -- listing alarms, creating the two
standard per-instance alarms, and reading a metric's recent average -- not a
general-purpose CloudWatch client wrapper.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, Self

from aws_admin_cli.domain.models.cloudwatch import MetricAlarm


class CloudWatchGateway(Protocol):
    """Port: CloudWatch alarm and metric operations."""

    def describe_alarms(self: Self) -> list[MetricAlarm]:
        """List every metric alarm in this region (fully paginated)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def put_metric_alarm(
        self: Self,
        *,
        alarm_name: str,
        metric_name: str,
        namespace: str,
        dimensions: Mapping[str, str],
        statistic: str,
        period: int,
        evaluation_periods: int,
        threshold: float,
        comparison_operator: str,
    ) -> None:
        """Create (or update, if ``alarm_name`` already exists) one metric alarm."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_average_cpu_utilization(
        self: Self, instance_id: str, *, start: datetime, end: datetime
    ) -> float | None:
        """Average ``CPUUtilization`` for ``instance_id`` between ``start``/``end``.

        Returns ``None`` when CloudWatch has no datapoints for the window
        (a brand-new instance, or -- as on LocalStack -- an environment that
        doesn't simulate real metric data) rather than ``0.0``: "no data" and
        "confirmed idle" are different findings, and a caller must not
        conflate them.
        """
        ...  # pragma: no cover -- Protocol body, never executed

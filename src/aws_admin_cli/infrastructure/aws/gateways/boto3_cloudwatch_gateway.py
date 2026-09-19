"""The boto3-backed ``CloudWatchGateway`` implementation.

Every method is wrapped in :func:`aws_error_boundary`, so nothing from
botocore ever crosses back into ``application/``. ``describe_alarms`` is
fully paginated, same discipline every other listing gateway in this
codebase follows.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self, cast

from aws_admin_cli.domain.models.cloudwatch import MetricAlarm
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.error_mapper import aws_error_boundary

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aws_admin_cli.domain.ports.cloudwatch_gateway import CloudWatchGateway


@dataclass(frozen=True, slots=True)
class Boto3CloudWatchGateway:
    """``CloudWatchGateway`` implemented against a real (or LocalStack) CloudWatch client."""

    client_factory: ClientFactory

    def _client(self: Self) -> Any:
        return self.client_factory.cloudwatch()

    def describe_alarms(self: Self) -> list[MetricAlarm]:
        """List every metric alarm, fully paginated."""
        alarms: list[MetricAlarm] = []
        with aws_error_boundary("cloudwatch", "DescribeAlarms"):
            for page in self._client().get_paginator("describe_alarms").paginate():
                alarms.extend(MetricAlarm.model_validate(raw) for raw in page["MetricAlarms"])
        return alarms

    def put_metric_alarm(
        self: Self,
        *,
        alarm_name: str,
        metric_name: str,
        namespace: str,
        dimensions: "Mapping[str, str]",
        statistic: str,
        period: int,
        evaluation_periods: int,
        threshold: float,
        comparison_operator: str,
    ) -> None:
        """Create (or update) one metric alarm."""
        with aws_error_boundary("cloudwatch", "PutMetricAlarm"):
            self._client().put_metric_alarm(
                AlarmName=alarm_name,
                MetricName=metric_name,
                Namespace=namespace,
                Dimensions=[{"Name": key, "Value": value} for key, value in dimensions.items()],
                Statistic=statistic,
                Period=period,
                EvaluationPeriods=evaluation_periods,
                Threshold=threshold,
                ComparisonOperator=comparison_operator,
            )

    def get_average_cpu_utilization(
        self: Self, instance_id: str, *, start: datetime, end: datetime
    ) -> float | None:
        """Average ``CPUUtilization`` for ``instance_id`` over ``[start, end]``."""
        with aws_error_boundary("cloudwatch", "GetMetricStatistics"):
            response = self._client().get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=start,
                EndTime=end,
                Period=86400,
                Statistics=["Average"],
            )
        datapoints = response.get("Datapoints", [])
        if not datapoints:
            return None
        return cast(float, sum(point["Average"] for point in datapoints) / len(datapoints))


if TYPE_CHECKING:
    # Static conformance check: mypy fails right here if Boto3CloudWatchGateway's
    # method signatures ever drift from the CloudWatchGateway Protocol.
    _cloudwatch_gateway_conformance: CloudWatchGateway = cast(Boto3CloudWatchGateway, None)

"""CloudWatch domain models.

Same hydration pattern as the rest of ``domain/models/``: PascalCase alias
generator matching AWS's own casing, ``populate_by_name=True``, and
``extra="ignore"`` (``DescribeAlarms`` carries fields -- ``ActionsEnabled``,
``InsufficientDataActions``, ``TreatMissingData``, ... -- nothing here reads).
"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_pascal

__all__ = ["AlarmDimension", "MetricAlarm"]

_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="ignore",
    populate_by_name=True,
    alias_generator=to_pascal,
)


class AlarmDimension(BaseModel):
    """One ``{"Name": ..., "Value": ...}`` dimension pinning an alarm to a resource."""

    model_config = _MODEL_CONFIG

    name: str
    value: str


class MetricAlarm(BaseModel):
    """One CloudWatch metric alarm, from ``DescribeAlarms``' ``MetricAlarms[]``."""

    model_config = _MODEL_CONFIG

    alarm_name: str
    alarm_arn: str | None = None
    metric_name: str
    namespace: str
    state_value: str = "INSUFFICIENT_DATA"
    dimensions: list[AlarmDimension] = Field(default_factory=list)

    def dimension_value(self: Self, name: str) -> str | None:
        """The value of this alarm's ``name`` dimension, or ``None`` if it has none."""
        for dimension in self.dimensions:
            if dimension.name == name:
                return dimension.value
        return None

"""Cross-service domain models: the local resource ledger record."""

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class ResourceRecord(BaseModel):
    """A local record of one AWS resource this CLI created or is tracking.

    Persisted by ``JsonRepository`` so users can see, across profiles, what
    ``aws_admin_cli`` itself has provisioned — independent of (and a lot faster
    than) an AWS API call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_type: str
    identifier: str
    arn: str | None
    profile: str
    region: str
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self: Self) -> str:
        """Unique repository key: ``"<resource_type>:<identifier>"``."""
        return f"{self.resource_type}:{self.identifier}"

    @field_serializer("created_at")
    def _serialize_created_at(self: Self, value: datetime) -> str:
        """Serialize as ISO-8601 with a trailing ``Z`` rather than ``+00:00``."""
        return value.isoformat().replace("+00:00", "Z")

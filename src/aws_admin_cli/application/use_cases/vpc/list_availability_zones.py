"""Use case: list availability zones in the configured region."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.vpc import AvailabilityZone
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway


@dataclass(frozen=True, slots=True)
class ListAvailabilityZonesUseCase:
    """List availability zones. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: VpcGateway

    def execute(self: Self) -> list[AvailabilityZone]:
        """Return every availability zone in the configured region."""
        return self.gateway.describe_availability_zones()

"""Use case: list every VPC visible to this account."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.vpc import Vpc
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway


@dataclass(frozen=True, slots=True)
class ListVpcsUseCase:
    """List VPCs. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: VpcGateway

    def execute(self: Self) -> list[Vpc]:
        """Return every VPC visible to this account."""
        return self.gateway.describe_vpcs(None, None)

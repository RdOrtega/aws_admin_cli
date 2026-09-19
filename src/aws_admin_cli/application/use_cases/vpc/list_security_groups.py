"""Use case: list security groups, optionally scoped to a VPC."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.vpc import SecurityGroup
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway


@dataclass(frozen=True, slots=True)
class ListSecurityGroupsUseCase:
    """List security groups. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: VpcGateway

    def execute(
        self: Self, vpc_id: str | None, *, region: str | None = None
    ) -> list[SecurityGroup]:
        """Return security groups, optionally scoped to ``vpc_id``.

        ``region``, when given, targets that region's client instead of the
        profile-default one -- used to inspect the network of an instance
        outside the active session region.
        """
        return self.gateway.describe_security_groups(None, vpc_id, None, region=region)

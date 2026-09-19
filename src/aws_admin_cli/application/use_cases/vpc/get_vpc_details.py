"""Use case: aggregate a VPC's full picture (itself, its subnets, its security groups)."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.vpc import SecurityGroup, Subnet, Vpc
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway


@dataclass(frozen=True, slots=True)
class VpcDetails:
    """A VPC's aggregated details -- not a single AWS response shape."""

    vpc: Vpc
    subnets: list[Subnet]
    security_groups: list[SecurityGroup]

    @property
    def subnet_count(self: Self) -> int:
        """Number of subnets in this VPC."""
        return len(self.subnets)

    @property
    def security_group_count(self: Self) -> int:
        """Number of security groups in this VPC."""
        return len(self.security_groups)


@dataclass(frozen=True, slots=True)
class GetVpcDetailsUseCase:
    """Fetch a VPC plus its subnets and security groups in one call."""

    gateway: VpcGateway

    def execute(self: Self, vpc: Vpc) -> VpcDetails:
        """Return the aggregated details for ``vpc``.

        Args:
            vpc: The already-resolved VPC (typically via
                ``NetworkResolver.resolve_vpc``) to fetch details for.
        """
        subnets = self.gateway.describe_subnets(None, vpc.vpc_id, None)
        security_groups = self.gateway.describe_security_groups(None, vpc.vpc_id, None)
        return VpcDetails(vpc=vpc, subnets=subnets, security_groups=security_groups)

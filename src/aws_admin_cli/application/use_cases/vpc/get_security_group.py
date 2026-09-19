"""Use case: fetch one security group by its exact ID."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.core.exceptions import ResourceNotFoundError
from aws_admin_cli.domain.models.vpc import SecurityGroup
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway


@dataclass(frozen=True, slots=True)
class GetSecurityGroupUseCase:
    """Fetch a single security group by its canonical ``sg-...`` ID.

    Distinct from ``NetworkResolver.resolve_security_group``: this expects an
    exact, already-known ID, no GroupName/Name-tag fuzzy matching.
    """

    gateway: VpcGateway

    def execute(self: Self, group_id: str) -> SecurityGroup:
        """Return the security group with ``group_id``.

        Raises:
            ResourceNotFoundError: No security group with that ID exists.
        """
        matches = self.gateway.describe_security_groups([group_id], None, None)
        if not matches:
            raise ResourceNotFoundError(
                f"No existe ningún security group con id '{group_id}'.",
                hint="Usa `vpc sg list` para ver los security groups disponibles.",
            )
        return matches[0]

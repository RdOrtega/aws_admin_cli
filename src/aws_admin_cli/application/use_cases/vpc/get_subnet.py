"""Use case: fetch one subnet by its exact ID."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.core.exceptions import ResourceNotFoundError
from aws_admin_cli.domain.models.vpc import Subnet
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway


@dataclass(frozen=True, slots=True)
class GetSubnetUseCase:
    """Fetch a single subnet by its canonical ``subnet-...`` ID.

    Distinct from ``NetworkResolver.resolve_subnet``: this expects an exact,
    already-known ID (no Name-tag fuzzy matching, no ambiguity handling) --
    the direct-lookup counterpart Fase 5's EC2 use cases can reach for once
    they already hold a canonical ID, without paying for the resolver's
    broader (and cached) subnet listing.
    """

    gateway: VpcGateway

    def execute(self: Self, subnet_id: str) -> Subnet:
        """Return the subnet with ``subnet_id``.

        Raises:
            ResourceNotFoundError: No subnet with that ID exists.
        """
        matches = self.gateway.describe_subnets([subnet_id], None, None)
        if not matches:
            raise ResourceNotFoundError(
                f"No existe ninguna subnet con id '{subnet_id}'.",
                hint="Usa `vpc subnet list` para ver las subnets disponibles.",
            )
        return matches[0]

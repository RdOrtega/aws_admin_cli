"""Use case: list subnets, with VPC / AZ / public-private filters."""

import logging
from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.vpc import ListSubnetsRequest
from aws_admin_cli.domain.models.vpc import Subnet
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway


@dataclass(frozen=True, slots=True)
class ListSubnetsUseCase:
    """List subnets, filtering by VPC, availability zone, and/or public-vs-private."""

    gateway: VpcGateway
    logger: logging.Logger

    def execute(self: Self, request: ListSubnetsRequest) -> list[Subnet]:
        """Return subnets matching ``request``'s filters.

        A subnet whose ``is_public`` couldn't be determined (``None``) never
        matches ``public_only``/``private_only`` -- it's logged and skipped
        rather than silently guessed into either bucket.
        """
        subnets = self.gateway.describe_subnets(None, request.vpc_id, None)

        if request.availability_zone:
            subnets = [
                subnet
                for subnet in subnets
                if subnet.availability_zone == request.availability_zone
            ]

        if request.public_only or request.private_only:
            subnets = [subnet for subnet in subnets if self._matches_visibility(subnet, request)]

        return subnets

    def _matches_visibility(self: Self, subnet: Subnet, request: ListSubnetsRequest) -> bool:
        if subnet.is_public is None:
            self.logger.warning(
                "No se pudo determinar si la subnet %s es pública o privada; "
                "se omite del filtro --public/--private.",
                subnet.subnet_id,
            )
            return False
        if request.public_only:
            return subnet.is_public
        return not subnet.is_public

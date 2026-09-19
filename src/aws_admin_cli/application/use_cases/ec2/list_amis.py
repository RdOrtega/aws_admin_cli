"""Use case: list AMIs, optionally scoped by owner or name pattern."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.ec2 import Ami
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class ListAmisUseCase:
    """List AMIs. Kept as a use case so the CLI never touches the gateway directly."""

    gateway: Ec2Gateway

    def execute(self: Self, owner: str | None = None, name_pattern: str | None = None) -> list[Ami]:
        """Return AMIs, optionally scoped by ``owner`` and/or an EC2-filter ``name_pattern``."""
        owners = [owner] if owner else None
        filters = {"name": [name_pattern]} if name_pattern else None
        return self.gateway.describe_images(None, owners, filters)

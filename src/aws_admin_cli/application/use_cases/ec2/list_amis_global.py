"""Use case: list AMIs across every AWS region, for the AMI Management screen."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.ec2 import Ami
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class ListAmisGlobalUseCase:
    """Same filters as ``ListAmisUseCase``, scanned across every region at once.

    The active session region (``ctx.settings.region``) never enters into
    this -- it stays the target for new-instance creation only. Each
    returned ``Ami.region`` records where it was actually found.
    """

    gateway: Ec2Gateway

    def execute(self: Self, owner: str | None = None, name_pattern: str | None = None) -> list[Ami]:
        """Return AMIs, optionally scoped by ``owner`` and/or an EC2-filter ``name_pattern``."""
        owners = [owner] if owner else None
        filters = {"name": [name_pattern]} if name_pattern else None
        return self.gateway.describe_images_all_regions(owners, filters)

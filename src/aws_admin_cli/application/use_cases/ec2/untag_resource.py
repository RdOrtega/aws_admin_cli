"""Use case: remove tags (by key) from any EC2 resource, by ID."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class UntagEc2ResourceUseCase:
    """Remove tags (by key) from any EC2 resource ID -- instance, AMI, or otherwise.

    See ``TagEc2ResourceUseCase`` for why this exists separately from
    ``DeleteInstanceTagsUseCase``.
    """

    gateway: Ec2Gateway

    def execute(
        self: Self, resource_id: str, keys: Sequence[str], *, region: str | None = None
    ) -> None:
        """Remove ``keys`` from ``resource_id``. A no-op if ``keys`` is empty.

        ``region``, when given, targets the resource's actual region instead
        of the profile-default one.
        """
        self.gateway.delete_tags([resource_id], keys, region=region)

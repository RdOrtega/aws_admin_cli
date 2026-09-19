"""Use case: tag any EC2 resource (instance, AMI, ...) by ID."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class TagEc2ResourceUseCase:
    """Add/update tags on any EC2 resource ID -- instance, AMI, or otherwise.

    ``CreateTags`` is resource-agnostic by ID prefix. This exists separately
    from ``SetInstanceTagsUseCase`` because that one also refetches the
    tagged resource as an ``Instance`` afterward, which breaks for anything
    that isn't actually an instance (e.g. an AMI).
    """

    gateway: Ec2Gateway

    def execute(
        self: Self, resource_id: str, tags: Mapping[str, str], *, region: str | None = None
    ) -> None:
        """Apply ``tags`` to ``resource_id``. A no-op if ``tags`` is empty.

        ``region``, when given, targets the resource's actual region instead
        of the profile-default one.
        """
        self.gateway.create_tags([resource_id], tags, region=region)

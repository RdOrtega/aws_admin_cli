"""Use case: add or overwrite tags on an EC2 instance."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.ec2 import SetInstanceTagsRequest
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class SetInstanceTagsUseCase:
    """Add/update tags on an instance. A no-op if ``request.tags`` is empty."""

    gateway: Ec2Gateway

    def execute(self: Self, request: SetInstanceTagsRequest) -> Instance:
        """Apply ``request.tags`` to ``request.instance_id`` and return the refreshed instance."""
        self.gateway.create_tags([request.instance_id], request.tags)
        return self.gateway.get_instance(request.instance_id)

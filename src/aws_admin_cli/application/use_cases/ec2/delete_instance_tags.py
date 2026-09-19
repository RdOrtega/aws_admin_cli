"""Use case: remove tags (by key) from an EC2 instance."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.ec2 import DeleteInstanceTagsRequest
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class DeleteInstanceTagsUseCase:
    """Remove tags on an instance. A no-op if ``request.keys`` is empty."""

    gateway: Ec2Gateway

    def execute(self: Self, request: DeleteInstanceTagsRequest) -> Instance:
        """Remove ``request.keys`` from the instance, then return the refreshed instance."""
        self.gateway.delete_tags([request.instance_id], request.keys)
        return self.gateway.get_instance(request.instance_id)

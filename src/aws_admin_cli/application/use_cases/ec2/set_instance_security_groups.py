"""Use case: replace the security groups attached to a running EC2 instance."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.ec2 import SetInstanceSecurityGroupsRequest
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class SetInstanceSecurityGroupsUseCase:
    """Attach ``request.group_ids`` to the instance, replacing whatever it had."""

    gateway: Ec2Gateway

    def execute(
        self: Self, request: SetInstanceSecurityGroupsRequest, *, region: str | None = None
    ) -> Instance:
        """Apply the new group set and return the refreshed instance."""
        self.gateway.set_instance_security_groups(
            request.instance_id, request.group_ids, region=region
        )
        return self.gateway.get_instance(request.instance_id, region=region)

"""Use case: create an AMI from an existing instance."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.ec2 import CreateAmiRequest
from aws_admin_cli.domain.models.ec2 import Ami
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class CreateAmiUseCase:
    """Create an AMI from ``request.instance_id``. AWS itself validates the source instance."""

    gateway: Ec2Gateway

    def execute(self: Self, request: CreateAmiRequest, *, region: str | None = None) -> Ami:
        """Create and return the new AMI.

        ``region``, when given, targets the source instance's actual region
        instead of the profile-default one.
        """
        return self.gateway.create_image(
            request.instance_id,
            request.name,
            request.description,
            no_reboot=request.no_reboot,
            tags=request.tags or None,
            region=region,
        )

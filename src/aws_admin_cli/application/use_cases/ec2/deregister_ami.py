"""Use case: deregister (delete) an AMI."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class DeregisterAmiUseCase:
    """Deregister an AMI by ID."""

    gateway: Ec2Gateway

    def execute(self: Self, image_id: str, *, region: str | None = None) -> None:
        """Deregister ``image_id``. Does not delete its backing EBS snapshot(s).

        ``region``, when given, routes the call through that region's client
        instead of the profile-default one.
        """
        self.gateway.deregister_image(image_id, region=region)

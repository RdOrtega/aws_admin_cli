"""Use case: fetch a single AMI by its image ID."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.ec2 import Ami
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class GetAmiUseCase:
    """Fetch one AMI by ID. AWS itself raises InvalidAMIID.NotFound for a bad id."""

    gateway: Ec2Gateway

    def execute(self: Self, image_id: str, *, region: str | None = None) -> Ami:
        """Return the AMI matching ``image_id``.

        ``region``, when given, routes the lookup through that region's
        client instead of the profile-default one.
        """
        images = self.gateway.describe_images([image_id], None, None, region=region)
        # AWS raises before this could ever be empty in practice -- same
        # reasoning as Boto3Ec2Gateway.get_instance.
        return images[0]

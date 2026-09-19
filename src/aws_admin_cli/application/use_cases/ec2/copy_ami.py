"""Use case: copy an AMI into another region, inheriting the source AMI's tags."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.ec2 import CopyAmiRequest
from aws_admin_cli.domain.models.ec2 import Ami
from aws_admin_cli.domain.models.vpc import tags_to_dict
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class CopyAmiUseCase:
    """Copy an AMI, programmatically inheriting the source AMI's tags onto the copy.

    ``source_region`` is this profile's configured region (where the source
    AMI lives) -- not something the caller re-supplies per request, same as
    ``LaunchInstanceUseCase.profile``/``region``.
    """

    gateway: Ec2Gateway
    source_region: str

    def execute(self: Self, request: CopyAmiRequest) -> Ami:
        """Copy the AMI and return the new one, in ``request.target_region``."""
        source = self.gateway.describe_images([request.source_image_id], None, None)
        tags = tags_to_dict(source[0].tags) if source else {}
        return self.gateway.copy_image(
            request.source_image_id,
            source_region=self.source_region,
            name=request.name,
            description=request.description,
            target_region=request.target_region,
            kms_key_id=request.kms_key_id,
            tags=tags or None,
        )

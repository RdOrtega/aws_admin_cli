"""Use case: enable or remove Block Public Access on an existing bucket."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class SetBucketPublicAccessUseCase:
    """Toggle a bucket's Block Public Access.

    Kept as a use case so the CLI never calls the gateway directly.
    """

    gateway: S3Gateway

    def execute(self: Self, name: str, *, block: bool) -> None:
        """Set all 4 Block Public Access flags to ``block`` on ``name``."""
        self.gateway.set_public_access_block(name, block=block)

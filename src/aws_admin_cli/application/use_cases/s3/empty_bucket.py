"""Use case: purge every object/version from a bucket, keeping the bucket itself."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class EmptyBucketUseCase:
    """Empty a bucket. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: S3Gateway

    def execute(self: Self, name: str) -> None:
        """Delete every object/version/delete-marker in ``name``, keeping the bucket."""
        self.gateway.empty_bucket(name)

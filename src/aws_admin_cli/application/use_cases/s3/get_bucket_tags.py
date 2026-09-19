"""Use case: fetch a bucket's tags."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class GetBucketTagsUseCase:
    """Fetch bucket tags. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: S3Gateway

    def execute(self: Self, name: str) -> dict[str, str]:
        """Return the tags on bucket ``name``."""
        return self.gateway.get_bucket_tags(name)

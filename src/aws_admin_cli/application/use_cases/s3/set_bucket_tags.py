"""Use case: replace a bucket's tags."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import SetBucketTagsRequest
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class SetBucketTagsUseCase:
    """Set bucket tags. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: S3Gateway

    def execute(self: Self, request: SetBucketTagsRequest) -> None:
        """Replace the tags on ``request.name`` with ``request.tags``."""
        self.gateway.set_bucket_tags(request.name, request.tags)

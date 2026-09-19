"""Use case: enable or suspend a bucket's versioning."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import SetVersioningRequest
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class SetVersioningUseCase:
    """Set a bucket's versioning. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: S3Gateway

    def execute(self: Self, request: SetVersioningRequest) -> None:
        """Enable or suspend versioning on ``request.name``."""
        self.gateway.set_bucket_versioning(request.name, request.enabled)

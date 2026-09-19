"""Use case: copy an object server-side."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import CopyObjectRequest
from aws_admin_cli.domain.models.s3 import S3Object
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class CopyObjectUseCase:
    """Copy an object. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: S3Gateway

    def execute(self: Self, request: CopyObjectRequest) -> S3Object:
        """Copy ``request.src_bucket``/``request.src_key`` to the destination, server-side."""
        return self.gateway.copy_object(
            request.src_bucket, request.src_key, request.dst_bucket, request.dst_key
        )

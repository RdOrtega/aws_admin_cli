"""Use case: list a bucket's objects (delimiter-aware; "directories" are common prefixes)."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import ListObjectsRequest
from aws_admin_cli.domain.models.s3 import ObjectListing
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class ListObjectsUseCase:
    """List objects. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: S3Gateway

    def execute(self: Self, request: ListObjectsRequest) -> ObjectListing:
        """Return the listing for ``request.bucket``, per its prefix/delimiter/max_items."""
        return self.gateway.list_objects(
            request.bucket, request.prefix, request.delimiter, request.max_items
        )

"""Use case: list every bucket owned by this account."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.s3 import Bucket
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class ListBucketsUseCase:
    """List buckets. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: S3Gateway

    def execute(self: Self) -> list[Bucket]:
        """Return every bucket owned by this account."""
        return self.gateway.list_buckets()

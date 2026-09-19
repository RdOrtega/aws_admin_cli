"""Use case: delete a single S3 object."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import DeleteObjectRequest
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.ports.repository import Repository
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class DeleteObjectUseCase:
    """Delete a single object and remove it from the local ledger."""

    gateway: S3Gateway
    repository: Repository[ResourceRecord]

    def execute(self: Self, request: DeleteObjectRequest) -> None:
        """Delete ``request.bucket``/``request.key``."""
        self.gateway.delete_object(request.bucket, request.key)
        self.repository.delete(f"s3:object:{request.bucket}/{request.key}")

"""Use case: delete every object under a key prefix, batched."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import DeletePrefixRequest
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.ports.repository import Repository
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway

_DELETE_BATCH_SIZE = 1000  # S3's DeleteObjects API hard limit per request.


@dataclass(frozen=True, slots=True)
class DeletePrefixUseCase:
    """Delete every object under a prefix. A prefix matching nothing is not an error."""

    gateway: S3Gateway
    repository: Repository[ResourceRecord]

    def execute(self: Self, request: DeletePrefixRequest) -> int:
        """Delete every object under ``request.prefix``.

        Returns:
            How many objects were deleted (``0`` if the prefix matched nothing).
        """
        listing = self.gateway.list_objects(request.bucket, request.prefix, None, None)
        keys = [item.key for item in listing.objects]

        deleted: list[str] = []
        for start in range(0, len(keys), _DELETE_BATCH_SIZE):
            batch = keys[start : start + _DELETE_BATCH_SIZE]
            deleted.extend(self.gateway.delete_objects(request.bucket, batch))

        for key in deleted:
            self.repository.delete(f"s3:object:{request.bucket}/{key}")

        return len(deleted)

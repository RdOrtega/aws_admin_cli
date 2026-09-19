"""Use case: delete an S3 bucket, refusing to silently drop its contents."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import DeleteBucketRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.ports.repository import Repository
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway

_DELETE_BATCH_SIZE = 1000  # S3's DeleteObjects API hard limit per request.


@dataclass(frozen=True, slots=True)
class DeleteBucketUseCase:
    """Delete an S3 bucket: blocks on non-empty buckets unless ``force`` is set."""

    gateway: S3Gateway
    repository: Repository[ResourceRecord]

    def execute(self: Self, request: DeleteBucketRequest) -> None:
        """Delete the bucket.

        Args:
            request: The delete request.

        Raises:
            ValidationError: The bucket isn't empty and ``force`` is
                ``False``. Nothing is deleted in this case.
        """
        listing = self.gateway.list_objects(request.name, None, None, None)

        if listing.objects and not request.force:
            raise ValidationError(
                f"El bucket '{request.name}' no está vacío: contiene "
                f"{listing.key_count} objeto(s).",
                hint="Usa --force para vaciarlo y borrarlo de todas formas.",
            )

        keys = [item.key for item in listing.objects]
        for start in range(0, len(keys), _DELETE_BATCH_SIZE):
            batch = keys[start : start + _DELETE_BATCH_SIZE]
            deleted = self.gateway.delete_objects(request.name, batch)
            for key in deleted:
                # Keeps the local ledger from accumulating stale s3:object records
                # for objects that no longer exist, in a bucket that no longer
                # exists either -- upload_object.py is what creates these.
                self.repository.delete(f"s3:object:{request.name}/{key}")

        # `delete_bucket` itself is responsible for purging any leftover object
        # versions/delete markers (an S3 API quirk on versioned buckets) --
        # not this use case's concern.
        self.gateway.delete_bucket(request.name)
        self.repository.delete(f"s3:bucket:{request.name}")

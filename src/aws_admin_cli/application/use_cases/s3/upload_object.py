"""Use case: upload a local file as an S3 object, guarding against silent overwrites."""

import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from aws_admin_cli.application.dto.s3 import UploadObjectRequest
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.s3 import S3Object
from aws_admin_cli.domain.ports.repository import Repository
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway

_DEFAULT_CONTENT_TYPE = "application/octet-stream"


@dataclass(frozen=True, slots=True)
class UploadObjectUseCase:
    """Upload a local file: guesses content type, blocks silent overwrites, ledger-tracked."""

    gateway: S3Gateway
    repository: Repository[ResourceRecord]
    profile: str
    region: str

    def execute(
        self: Self,
        request: UploadObjectRequest,
        *,
        progress_callback: Callable[[int], None] | None = None,
    ) -> S3Object:
        """Upload ``request.source`` to ``request.bucket``/``request.key``.

        Args:
            request: The upload request.
            progress_callback: Optional callback invoked with bytes
                transferred per chunk, forwarded straight to the gateway.
                The domain/application layers never build a progress bar
                themselves -- that's `presentation/`'s job.

        Returns:
            The uploaded ``S3Object``.

        Raises:
            ValidationError: ``source`` doesn't exist or isn't a regular
                file, or the key already exists and ``overwrite`` is ``False``.
        """
        if not request.source.exists() or not request.source.is_file():
            raise ValidationError(
                f"'{request.source}' no existe o no es un archivo regular.",
                hint="Verifica la ruta local.",
            )

        if not request.overwrite and self._exists(request.bucket, request.key):
            raise ValidationError(
                f"El objeto '{request.key}' ya existe en '{request.bucket}'.",
                hint="Usa --overwrite para reemplazarlo.",
            )

        content_type = request.content_type
        if content_type is None:
            guessed, _ = mimetypes.guess_type(request.source.name)
            content_type = guessed or _DEFAULT_CONTENT_TYPE

        obj = self.gateway.upload_file(
            request.bucket,
            request.key,
            request.source,
            content_type=content_type,
            metadata=request.metadata or None,
            storage_class=request.storage_class,
            progress_callback=progress_callback,
        )
        self._track(obj, request)
        return obj

    def _exists(self: Self, bucket: str, key: str) -> bool:
        try:
            self.gateway.head_object(bucket, key)
        except ResourceNotFoundError:
            return False
        return True

    def _track(self: Self, obj: S3Object, request: UploadObjectRequest) -> None:
        self.repository.save(
            ResourceRecord(
                resource_type="s3:object",
                identifier=f"{request.bucket}/{obj.key}",
                arn=f"arn:aws:s3:::{request.bucket}/{obj.key}",
                profile=self.profile,
                region=self.region,
                created_at=datetime.now(UTC),
                metadata={"size": obj.size, "etag": obj.etag},
            )
        )

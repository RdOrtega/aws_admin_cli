"""Use case: upload a local directory tree recursively, as a key prefix."""

import fnmatch
from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import (
    UploadDirectoryRequest,
    UploadDirectorySummary,
    UploadObjectRequest,
)
from aws_admin_cli.application.use_cases.s3.upload_object import UploadObjectUseCase
from aws_admin_cli.core.exceptions import ValidationError


@dataclass(frozen=True, slots=True)
class UploadDirectoryUseCase:
    """Upload a directory tree: composes ``UploadObjectUseCase`` per file.

    Reuses ``UploadObjectUseCase`` rather than talking to ``S3Gateway``
    directly, so every file gets the exact same content-type detection,
    overwrite guard rail, and ledger tracking a single ``upload_object``
    command would give it -- no duplicated logic.
    """

    upload_object: UploadObjectUseCase

    def execute(self: Self, request: UploadDirectoryRequest) -> UploadDirectorySummary:
        """Upload every file under ``request.source_dir``.

        Args:
            request: The upload request.

        Returns:
            A summary: files uploaded, files skipped (matched ``exclude``),
            and total bytes uploaded.

        Raises:
            ValidationError: ``source_dir`` doesn't exist or isn't a directory.
        """
        if not request.source_dir.exists() or not request.source_dir.is_dir():
            raise ValidationError(
                f"'{request.source_dir}' no existe o no es un directorio.",
                hint="Verifica la ruta local.",
            )

        uploaded = 0
        skipped = 0
        total_bytes = 0

        for path in sorted(request.source_dir.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(request.source_dir).as_posix()
            if any(fnmatch.fnmatch(relative, pattern) for pattern in request.exclude):
                skipped += 1
                continue
            key = f"{request.prefix.rstrip('/')}/{relative}" if request.prefix else relative
            obj = self.upload_object.execute(
                UploadObjectRequest(
                    bucket=request.bucket,
                    key=key,
                    source=path,
                    storage_class=request.storage_class,
                    overwrite=request.overwrite,
                )
            )
            uploaded += 1
            total_bytes += obj.size

        return UploadDirectorySummary(uploaded=uploaded, skipped=skipped, total_bytes=total_bytes)

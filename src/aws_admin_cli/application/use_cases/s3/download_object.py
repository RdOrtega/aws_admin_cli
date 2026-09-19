"""Use case: download an S3 object to a local path, guarding against silent overwrites."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from aws_admin_cli.application.dto.s3 import DownloadObjectRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class DownloadObjectUseCase:
    """Download an object: resolves a directory destination, blocks silent overwrites."""

    gateway: S3Gateway

    def execute(
        self: Self,
        request: DownloadObjectRequest,
        *,
        progress_callback: Callable[[int], None] | None = None,
    ) -> Path:
        """Download ``request.bucket``/``request.key`` to ``request.destination``.

        Args:
            request: The download request.
            progress_callback: Optional callback invoked with bytes
                transferred per chunk, forwarded straight to the gateway.

        Returns:
            The final destination path (the key's basename, if
            ``request.destination`` was a directory).

        Raises:
            ValidationError: The resolved destination already exists and
                ``overwrite`` is ``False``.
        """
        destination = request.destination
        if destination.is_dir():
            destination = destination / Path(request.key).name

        if destination.exists() and not request.overwrite:
            raise ValidationError(
                f"'{destination}' ya existe.", hint="Usa --overwrite para reemplazarlo."
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        return self.gateway.download_file(
            request.bucket, request.key, destination, progress_callback
        )

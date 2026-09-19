"""Use case: generate a presigned URL, enforcing SigV4's expiration limits."""

import logging
from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.s3 import PresignUrlRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway

_MIN_EXPIRES_IN = 1
_MAX_EXPIRES_IN = 604800  # 7 days: SigV4's hard maximum.
_WARN_THRESHOLD_SECONDS = 86400  # 1 day.


@dataclass(frozen=True, slots=True)
class PresignUrlUseCase:
    """Generate a presigned URL: enforces SigV4's [1, 604800]s window, warns past 1 day."""

    gateway: S3Gateway
    logger: logging.Logger

    def execute(self: Self, request: PresignUrlRequest) -> str:
        """Generate the URL.

        Raises:
            ValidationError: ``expires_in`` is outside ``[1, 604800]`` seconds.
        """
        if not (_MIN_EXPIRES_IN <= request.expires_in <= _MAX_EXPIRES_IN):
            raise ValidationError(
                f"--expires-in debe estar entre {_MIN_EXPIRES_IN} y {_MAX_EXPIRES_IN} "
                f"segundos (recibido: {request.expires_in}).",
                hint="El máximo de SigV4 es 604800 segundos (7 días).",
            )
        if request.expires_in > _WARN_THRESHOLD_SECONDS:
            self.logger.warning(
                "URL prefirmada con una vida de %d segundos (> 1 día): cualquiera con "
                "el enlace tendrá acceso hasta que expire.",
                request.expires_in,
            )
        return self.gateway.generate_presigned_url(
            request.bucket, request.key, request.expires_in, request.method
        )

"""Use case: create an S3 bucket, secure by default, idempotent, ledger-tracked."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from aws_admin_cli.application.dto.s3 import CreateBucketRequest
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.s3 import Bucket, VersioningStatus, validate_bucket_name
from aws_admin_cli.domain.ports.repository import Repository
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class CreateBucketUseCase:
    """Create an S3 bucket: Block Public Access by default, idempotent, ledger-tracked."""

    gateway: S3Gateway
    repository: Repository[ResourceRecord]
    profile: str
    logger: logging.Logger

    def execute(self: Self, request: CreateBucketRequest) -> Bucket:
        """Create the bucket, or return the existing one if ``if_not_exists`` and it exists.

        Args:
            request: The create request.

        Returns:
            The created ``Bucket`` -- or, when ``if_not_exists`` is set and a
            bucket by that name already exists, that pre-existing ``Bucket``.
        """
        validate_bucket_name(request.name)

        if request.if_not_exists and self.gateway.bucket_exists(request.name):
            existing = self._find_existing(request.name)
            if existing is not None:
                return existing

        block_public_access = not request.allow_public
        if request.allow_public:
            self.logger.warning(
                "El bucket '%s' se crea SIN Block Public Access (--allow-public): "
                "puede quedar expuesto a internet si además le pones una política pública.",
                request.name,
            )

        bucket = self.gateway.create_bucket(
            request.name, request.region, block_public_access=block_public_access
        )

        if request.enable_versioning:
            self.gateway.set_bucket_versioning(request.name, True)

        self._track(bucket, request, versioning_enabled=request.enable_versioning)
        return bucket

    def _find_existing(self: Self, name: str) -> Bucket | None:
        return next((bucket for bucket in self.gateway.list_buckets() if bucket.name == name), None)

    def _track(
        self: Self, bucket: Bucket, request: CreateBucketRequest, *, versioning_enabled: bool
    ) -> None:
        self.repository.save(
            ResourceRecord(
                resource_type="s3:bucket",
                identifier=bucket.name,
                arn=f"arn:aws:s3:::{bucket.name}",
                profile=self.profile,
                region=bucket.region or request.region,
                created_at=datetime.now(UTC),
                metadata={
                    "region": bucket.region or request.region,
                    "versioning": (
                        VersioningStatus.ENABLED.value
                        if versioning_enabled
                        else VersioningStatus.DISABLED.value
                    ),
                },
            )
        )

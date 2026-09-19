"""Use case: a security/compliance snapshot of every bucket in the account."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.s3 import VersioningStatus
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class BucketAuditEntry:
    """One bucket's compliance snapshot: public access, encryption, versioning, size."""

    name: str
    public_access_blocked: bool
    encryption: str | None
    versioning: VersioningStatus
    object_count: int
    total_size: int


@dataclass(frozen=True, slots=True)
class AuditBucketsUseCase:
    """Scan every bucket for Block Public Access, default encryption, and versioning."""

    gateway: S3Gateway

    def execute(self: Self) -> list[BucketAuditEntry]:
        """Return one ``BucketAuditEntry`` per bucket in the account."""
        entries = []
        for bucket in self.gateway.list_buckets():
            versioning = self.gateway.get_bucket_versioning(bucket.name)
            listing = self.gateway.list_objects(bucket.name, None, None, None)
            entries.append(
                BucketAuditEntry(
                    name=bucket.name,
                    public_access_blocked=self.gateway.get_public_access_block(bucket.name),
                    encryption=self.gateway.get_bucket_encryption(bucket.name),
                    versioning=versioning.status,
                    object_count=listing.key_count,
                    total_size=listing.total_size,
                )
            )
        return entries

"""Use case: gather a bucket's full info (region, versioning, policy, tags, object count)."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.policy import PolicyDocument
from aws_admin_cli.domain.models.s3 import BucketVersioning
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class BucketInfo:
    """A bucket's info, aggregated from several S3 calls -- not a single AWS response shape."""

    name: str
    region: str
    versioning: BucketVersioning
    policy: PolicyDocument | None
    tags: dict[str, str]
    object_count: int


@dataclass(frozen=True, slots=True)
class GetBucketInfoUseCase:
    """Fetch a bucket's region, versioning, policy, tags, and object count in one call."""

    gateway: S3Gateway

    def execute(self: Self, name: str) -> BucketInfo:
        """Return the aggregated info for bucket ``name``."""
        region = self.gateway.get_bucket_location(name)
        versioning = self.gateway.get_bucket_versioning(name)
        policy = self.gateway.get_bucket_policy(name)
        tags = self.gateway.get_bucket_tags(name)
        listing = self.gateway.list_objects(name, None, None, None)
        return BucketInfo(
            name=name,
            region=region,
            versioning=versioning,
            policy=policy,
            tags=tags,
            object_count=listing.key_count,
        )

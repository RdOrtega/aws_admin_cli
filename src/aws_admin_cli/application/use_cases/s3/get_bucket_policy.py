"""Use case: fetch a bucket's policy."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.policy import PolicyDocument
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class GetBucketPolicyUseCase:
    """Fetch a bucket policy. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: S3Gateway

    def execute(self: Self, name: str) -> PolicyDocument | None:
        """Return the policy for bucket ``name``, or ``None`` if it has none."""
        return self.gateway.get_bucket_policy(name)

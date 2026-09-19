"""Use case: remove a bucket's policy."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class DeleteBucketPolicyUseCase:
    """Remove a bucket policy. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: S3Gateway

    def execute(self: Self, name: str) -> None:
        """Remove the policy on bucket ``name``, if it has one."""
        self.gateway.delete_bucket_policy(name)

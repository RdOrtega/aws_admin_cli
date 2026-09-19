"""Use case: whether a bucket is genuinely publicly accessible."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.core.exceptions import AwsAdminCliError
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway


@dataclass(frozen=True, slots=True)
class GetBucketAccessUseCase:
    """Whether a bucket is Public or Private: Block Public Access settles it outright.

    Public means "Block Public Access isn't fully locked down" -- any of the
    4 block flags set to ``False``, or no ``PublicAccessBlockConfiguration``
    at all (``NoSuchPublicAccessBlockConfiguration``), reads as Public,
    matching ``AuditBucketsUseCase``'s ``public_access_blocked`` semantics.
    A bucket created with Block Public Access off is Public immediately,
    with no separate bucket-policy grant required.

    Any AWS error resolving this (a permissions gap being the realistic one
    -- ``s3:GetBucketPublicAccessBlock`` denied) defaults safely to Private:
    this is a display badge, not a security gate, and would rather
    under-report than crash the bucket list.
    """

    gateway: S3Gateway

    def execute(self: Self, name: str) -> bool:
        """Return whether ``name`` allows public access; ``False`` on any error too."""
        try:
            return not self.gateway.get_public_access_block(name)
        except AwsAdminCliError:
            return False

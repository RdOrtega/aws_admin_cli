"""Use case: Public/Private status for every bucket in one pass."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.use_cases.s3.get_bucket_access import GetBucketAccessUseCase
from aws_admin_cli.domain.models.s3 import Bucket


@dataclass(frozen=True, slots=True)
class ListBucketAccessUseCase:
    """Resolve Public/Private for a whole bucket list, one ``GetBucketAccessUseCase`` call each.

    Takes the caller's already-fetched ``buckets`` (rather than listing them
    again itself) so the S3 screen's own ``ListBuckets`` call -- already made
    to build that list in the first place -- is never duplicated.
    """

    get_bucket_access: GetBucketAccessUseCase

    def execute(self: Self, buckets: list[Bucket]) -> dict[str, bool]:
        """Return ``{bucket.name: is_public}`` for every bucket in ``buckets``."""
        return {bucket.name: self.get_bucket_access.execute(bucket.name) for bucket in buckets}

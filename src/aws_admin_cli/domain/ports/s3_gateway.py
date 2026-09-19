"""The S3Gateway port: S3 operations, returning domain models only.

Implementations (``Boto3S3Gateway``) must never leak a raw AWS SDK value
across this boundary -- every method returns an
``aws_admin_cli.domain.models.s3`` model (or ``None``/``list``/``str``/
``Path``/nothing), and raises only ``aws_admin_cli.core.exceptions`` types
(via ``infrastructure.aws.error_mapper.aws_error_boundary``), never a raw
client-error exception from the underlying SDK.

The domain knows nothing about progress bars: ``progress_callback`` is just a
plain callable the implementation invokes with bytes-transferred-this-chunk
(as boto3's own S3 transfer manager delivers it). Rendering that as a
``rich.progress.Progress`` bar lives entirely in ``presentation/``.
"""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol, Self

from aws_admin_cli.domain.models.policy import PolicyDocument
from aws_admin_cli.domain.models.s3 import Bucket, BucketVersioning, ObjectListing, S3Object


class S3Gateway(Protocol):
    """Port: S3 operations for buckets and objects."""

    # -- Buckets ----------------------------------------------------------------

    def create_bucket(
        self: Self, name: str, region: str, *, block_public_access: bool
    ) -> Bucket:
        """Create a bucket."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_buckets(self: Self) -> list[Bucket]:
        """List every bucket owned by this account."""
        ...  # pragma: no cover -- Protocol body, never executed

    def bucket_exists(self: Self, name: str) -> bool:
        """Return whether ``name`` exists (and is accessible)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_bucket_location(self: Self, name: str) -> str:
        """Return the bucket's region (normalized: never ``None``/empty for us-east-1)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_bucket(self: Self, name: str) -> None:
        """Delete a bucket.

        Implementations are responsible for making this succeed even when the
        bucket has (or ever had) versioning enabled: real S3 refuses to delete
        a bucket that still holds noncurrent object versions or delete
        markers, even after every *current* object has been removed via
        ``delete_objects``, so a version/marker purge belongs here -- it's an
        S3 API quirk, not something callers should need to know about.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def get_bucket_versioning(self: Self, name: str) -> BucketVersioning:
        """Fetch a bucket's versioning configuration."""
        ...  # pragma: no cover -- Protocol body, never executed

    def set_bucket_versioning(self: Self, name: str, enabled: bool) -> None:
        """Enable or suspend versioning on a bucket."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_public_access_block(self: Self, name: str) -> bool:
        """Whether Block Public Access is fully enabled, or not configured at all."""
        ...  # pragma: no cover -- Protocol body, never executed

    def set_public_access_block(self: Self, name: str, *, block: bool) -> None:
        """Set all 4 Block Public Access flags to ``block`` on an existing bucket."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_bucket_encryption(self: Self, name: str) -> str | None:
        """The bucket's default SSE algorithm (e.g. ``"AES256"``), or ``None`` if unset."""
        ...  # pragma: no cover -- Protocol body, never executed

    def has_lifecycle_policy(self: Self, name: str) -> bool:
        """Whether the bucket has at least one lifecycle rule configured."""
        ...  # pragma: no cover -- Protocol body, never executed

    def empty_bucket(self: Self, name: str) -> None:
        """Delete every object/version/delete-marker in place, keeping the bucket itself."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_bucket_policy(self: Self, name: str) -> PolicyDocument | None:
        """Fetch a bucket's policy, or ``None`` if it has none."""
        ...  # pragma: no cover -- Protocol body, never executed

    def set_bucket_policy(self: Self, name: str, document: PolicyDocument) -> None:
        """Replace a bucket's policy."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_bucket_policy(self: Self, name: str) -> None:
        """Remove a bucket's policy, if it has one."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_bucket_tags(self: Self, name: str) -> dict[str, str]:
        """Fetch a bucket's tags."""
        ...  # pragma: no cover -- Protocol body, never executed

    def set_bucket_tags(self: Self, name: str, tags: dict[str, str]) -> None:
        """Replace a bucket's tags."""
        ...  # pragma: no cover -- Protocol body, never executed

    # -- Objects ------------------------------------------------------------------

    def list_objects(
        self: Self,
        bucket: str,
        prefix: str | None,
        delimiter: str | None,
        max_items: int | None,
    ) -> ObjectListing:
        """List objects (fully paginated up to ``max_items``, or unbounded if ``None``)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def object_exists(self: Self, bucket: str, key: str) -> bool:
        """Return whether ``key`` exists in ``bucket``."""
        ...  # pragma: no cover -- Protocol body, never executed

    def head_object(self: Self, bucket: str, key: str) -> S3Object:
        """Fetch an object's metadata (no body)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def upload_file(
        self: Self,
        bucket: str,
        key: str,
        source: Path,
        *,
        content_type: str | None,
        metadata: dict[str, str] | None,
        storage_class: str | None,
        progress_callback: Callable[[int], None] | None,
    ) -> S3Object:
        """Upload a local file, streaming (multipart when large), never loading it whole."""
        ...  # pragma: no cover -- Protocol body, never executed

    def download_file(
        self: Self,
        bucket: str,
        key: str,
        destination: Path,
        progress_callback: Callable[[int], None] | None,
    ) -> Path:
        """Download an object to a local file, streaming."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_object(self: Self, bucket: str, key: str) -> None:
        """Delete a single object."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_objects(self: Self, bucket: str, keys: Sequence[str]) -> list[str]:
        """Delete multiple objects (batched internally). Returns the keys actually deleted."""
        ...  # pragma: no cover -- Protocol body, never executed

    def copy_object(
        self: Self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
    ) -> S3Object:
        """Copy an object server-side, without downloading/re-uploading its bytes."""
        ...  # pragma: no cover -- Protocol body, never executed

    def generate_presigned_url(
        self: Self, bucket: str, key: str, expires_in: int, method: Literal["get", "put"]
    ) -> str:
        """Generate a presigned URL for a GET or PUT of ``bucket/key``."""
        ...  # pragma: no cover -- Protocol body, never executed

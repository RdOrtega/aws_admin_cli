"""The boto3-backed ``S3Gateway`` implementation.

Every method is wrapped in :func:`aws_error_boundary`, so nothing from
botocore ever crosses back into ``application/``. Listing operations are
fully paginated (``client.get_paginator(...)``) -- a ``list_objects`` that
only returns the first page would be a correctness bug, not a simplification.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, cast

from boto3.s3.transfer import TransferConfig

from aws_admin_cli.core.exceptions import AwsError, ResourceNotFoundError
from aws_admin_cli.domain.models.policy import PolicyDocument
from aws_admin_cli.domain.models.s3 import Bucket, BucketVersioning, ObjectListing, S3Object
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.error_mapper import aws_error_boundary

if TYPE_CHECKING:
    from aws_admin_cli.domain.ports.s3_gateway import S3Gateway

_logger = logging.getLogger("aws_admin_cli")

_US_EAST_1 = "us-east-1"
_MULTIPART_THRESHOLD = 8 * 1024 * 1024
_MAX_CONCURRENCY = 4
_DELETE_BATCH_SIZE = 1000  # S3's DeleteObjects API hard limit per request.
_PUBLIC_ACCESS_BLOCK_CONFIG = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}


def _create_bucket_kwargs(name: str, region: str) -> dict[str, Any]:
    """Build ``create_bucket()`` kwargs, working around S3's us-east-1 API inconsistency.

    ``us-east-1`` is S3's implicit default region and must NOT be named via
    ``CreateBucketConfiguration`` -- sending it produces
    ``InvalidLocationConstraint``. Every other region must be named
    explicitly via ``LocationConstraint``. Isolated here so it's testable
    without any network access.
    """
    kwargs: dict[str, Any] = {"Bucket": name}
    if region != _US_EAST_1:
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    return kwargs


def _transfer_config() -> TransferConfig:
    return TransferConfig(
        multipart_threshold=_MULTIPART_THRESHOLD, max_concurrency=_MAX_CONCURRENCY
    )


@dataclass(frozen=True, slots=True)
class Boto3S3Gateway:
    """``S3Gateway`` implemented against a real (or LocalStack) S3 client."""

    client_factory: ClientFactory

    def _client(self: Self) -> Any:
        # `ClientFactory.s3()` already caches the underlying boto3 client (and
        # applies the path-style-addressing override when endpoint_url is set),
        # so calling this per-method is cheap.
        return self.client_factory.s3()

    # -- Buckets ----------------------------------------------------------------

    def create_bucket(self: Self, name: str, region: str, *, block_public_access: bool) -> Bucket:
        """Create a bucket: SSE-S3 always applied, Block Public Access ALWAYS set explicitly.

        ``block_public_access`` picks ``True`` (all 4 flags on) or ``False``
        (all 4 flags off) -- never "leave it unset". A freshly created
        bucket with no ``PublicAccessBlockConfiguration`` at all is
        indistinguishable, from ``get_public_access_block``'s perspective,
        from one that's genuinely public (AWS's own
        ``NoSuchPublicAccessBlockConfiguration`` "ghost state"); calling
        ``PutPublicAccessBlock`` unconditionally here means "Allow Public
        Access" is a deliberate, explicit API call, not an accident of
        omission.
        """
        with aws_error_boundary("s3", "CreateBucket"):
            self._client().create_bucket(**_create_bucket_kwargs(name, region))

        self._try_enable_default_encryption(name)
        self._try_set_public_access_block(name, block=block_public_access)

        return Bucket(name=name, creation_date=datetime.now(UTC), region=region)

    def _try_set_public_access_block(self: Self, name: str, *, block: bool) -> None:
        """Apply Block Public Access (``block``, explicitly, all 4 flags).

        Degrades to a warning if unsupported. Scoped to this one operation
        only: moto supports it, but some
        LocalStack versions don't -- and an unsupported *this* call must
        never break bucket creation. This is not a general "swallow S3
        errors" pattern; every other method in this class (including
        ``set_public_access_block`` itself, called here) lets errors
        propagate normally.
        """
        try:
            self.set_public_access_block(name, block=block)
        except AwsError as exc:
            _logger.warning(
                "No se pudo aplicar Block Public Access a '%s' (¿no soportado por este "
                "endpoint?): %s",
                name,
                exc,
            )

    def _try_enable_default_encryption(self: Self, name: str) -> None:
        """Apply SSE-S3 (``AES256``) by default, degrading to a warning if unsupported.

        Always applied, unconditionally, unlike Block Public Access (which the
        caller can opt out of) -- there is no "create an unencrypted bucket"
        option in this CLI. Same scoped try/except as
        ``_try_block_public_access``: an endpoint that doesn't support
        ``PutBucketEncryption`` must never block bucket creation over it.
        """
        try:
            with aws_error_boundary("s3", "PutBucketEncryption"):
                self._client().put_bucket_encryption(
                    Bucket=name,
                    ServerSideEncryptionConfiguration={
                        "Rules": [
                            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
                        ]
                    },
                )
        except AwsError as exc:
            _logger.warning(
                "No se pudo aplicar cifrado SSE-S3 por defecto a '%s' (¿no soportado por "
                "este endpoint?): %s",
                name,
                exc,
            )

    def list_buckets(self: Self) -> list[Bucket]:
        """List every bucket owned by this account."""
        with aws_error_boundary("s3", "ListBuckets"):
            response = self._client().list_buckets()
        return [Bucket.model_validate(raw) for raw in response["Buckets"]]

    def bucket_exists(self: Self, name: str) -> bool:
        """Return whether ``name`` exists (and is accessible). A 403 still propagates."""
        try:
            with aws_error_boundary("s3", "HeadBucket"):
                self._client().head_bucket(Bucket=name)
        except ResourceNotFoundError:
            return False
        return True

    def get_bucket_location(self: Self, name: str) -> str:
        """Return the bucket's region, normalizing S3's ``None``-means-us-east-1 quirk."""
        with aws_error_boundary("s3", "GetBucketLocation"):
            response = self._client().get_bucket_location(Bucket=name)
        return response.get("LocationConstraint") or _US_EAST_1

    def delete_bucket(self: Self, name: str) -> None:
        """Delete a bucket, purging any leftover object versions/delete markers first."""
        self._purge_all_versions(name)
        with aws_error_boundary("s3", "DeleteBucket"):
            self._client().delete_bucket(Bucket=name)

    def _purge_all_versions(self: Self, name: str) -> None:
        """Delete every object version and delete marker, batched.

        Works uniformly whether versioning is/was enabled or not (a
        never-versioned object simply has ``VersionId="null"``) -- this is
        what keeps ``delete_bucket`` from failing with ``BucketNotEmpty`` on
        a bucket that's versioned but whose *current* objects were already
        removed by the caller.
        """
        client = self._client()
        to_delete: list[dict[str, str]] = []
        with aws_error_boundary("s3", "ListObjectVersions"):
            for page in client.get_paginator("list_object_versions").paginate(Bucket=name):
                to_delete.extend(
                    {"Key": version["Key"], "VersionId": version["VersionId"]}
                    for version in page.get("Versions", [])
                )
                to_delete.extend(
                    {"Key": marker["Key"], "VersionId": marker["VersionId"]}
                    for marker in page.get("DeleteMarkers", [])
                )

        for start in range(0, len(to_delete), _DELETE_BATCH_SIZE):
            batch = to_delete[start : start + _DELETE_BATCH_SIZE]
            with aws_error_boundary("s3", "DeleteObjects"):
                client.delete_objects(Bucket=name, Delete={"Objects": batch, "Quiet": True})

    def get_bucket_versioning(self: Self, name: str) -> BucketVersioning:
        """Fetch a bucket's versioning configuration."""
        with aws_error_boundary("s3", "GetBucketVersioning"):
            response = self._client().get_bucket_versioning(Bucket=name)
        if "Status" not in response:
            # Never configured: AWS omits the key entirely rather than sending "Disabled".
            return BucketVersioning()
        return BucketVersioning.model_validate(response)

    def get_public_access_block(self: Self, name: str) -> bool:
        """Whether every Block Public Access setting is on, or unconfigured entirely."""
        try:
            with aws_error_boundary("s3", "GetPublicAccessBlock"):
                response = self._client().get_public_access_block(Bucket=name)
        except ResourceNotFoundError as exc:
            if exc.aws_code == "NoSuchPublicAccessBlockConfiguration":
                return False
            raise
        config = response.get("PublicAccessBlockConfiguration", {})
        return all(config.get(key, False) for key in _PUBLIC_ACCESS_BLOCK_CONFIG)

    def set_public_access_block(self: Self, name: str, *, block: bool) -> None:
        """Set all 4 Block Public Access flags to ``block`` on an existing bucket."""
        config = {key: block for key in _PUBLIC_ACCESS_BLOCK_CONFIG}
        with aws_error_boundary("s3", "PutPublicAccessBlock"):
            self._client().put_public_access_block(
                Bucket=name, PublicAccessBlockConfiguration=config
            )

    def get_bucket_encryption(self: Self, name: str) -> str | None:
        """The bucket's default SSE algorithm (``"AES256"``/``"aws:kms"``), or ``None``."""
        try:
            with aws_error_boundary("s3", "GetBucketEncryption"):
                response = self._client().get_bucket_encryption(Bucket=name)
        except ResourceNotFoundError as exc:
            if exc.aws_code == "ServerSideEncryptionConfigurationNotFoundError":
                return None
            raise
        rules = response.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        if not rules:
            return None
        algorithm = rules[0].get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
        return cast(str | None, algorithm)

    def has_lifecycle_policy(self: Self, name: str) -> bool:
        """Whether the bucket has at least one lifecycle rule configured."""
        try:
            with aws_error_boundary("s3", "GetBucketLifecycleConfiguration"):
                response = self._client().get_bucket_lifecycle_configuration(Bucket=name)
        except ResourceNotFoundError as exc:
            if exc.aws_code == "NoSuchLifecycleConfiguration":
                return False
            raise
        return bool(response.get("Rules"))

    def empty_bucket(self: Self, name: str) -> None:
        """Delete every object/version/delete-marker in place, keeping the bucket itself."""
        self._purge_all_versions(name)

    def set_bucket_versioning(self: Self, name: str, enabled: bool) -> None:
        """Enable or suspend versioning on a bucket."""
        status = "Enabled" if enabled else "Suspended"
        with aws_error_boundary("s3", "PutBucketVersioning"):
            self._client().put_bucket_versioning(
                Bucket=name, VersioningConfiguration={"Status": status}
            )

    def get_bucket_policy(self: Self, name: str) -> PolicyDocument | None:
        """Fetch a bucket's policy, translating "no policy set" to ``None``."""
        try:
            with aws_error_boundary("s3", "GetBucketPolicy"):
                response = self._client().get_bucket_policy(Bucket=name)
        except ResourceNotFoundError as exc:
            if exc.aws_code == "NoSuchBucketPolicy":
                return None
            raise  # e.g. NoSuchBucket: the bucket itself is missing -- a real error.
        return PolicyDocument.from_aws(response["Policy"])

    def set_bucket_policy(self: Self, name: str, document: PolicyDocument) -> None:
        """Replace a bucket's policy."""
        with aws_error_boundary("s3", "PutBucketPolicy"):
            self._client().put_bucket_policy(Bucket=name, Policy=document.to_aws_json())

    def delete_bucket_policy(self: Self, name: str) -> None:
        """Remove a bucket's policy, if it has one."""
        with aws_error_boundary("s3", "DeleteBucketPolicy"):
            self._client().delete_bucket_policy(Bucket=name)

    def get_bucket_tags(self: Self, name: str) -> dict[str, str]:
        """Fetch a bucket's tags, translating "no tags set" to an empty dict."""
        try:
            with aws_error_boundary("s3", "GetBucketTagging"):
                response = self._client().get_bucket_tagging(Bucket=name)
        except ResourceNotFoundError as exc:
            if exc.aws_code == "NoSuchTagSet":
                return {}
            raise
        return {tag["Key"]: tag["Value"] for tag in response.get("TagSet", [])}

    def set_bucket_tags(self: Self, name: str, tags: dict[str, str]) -> None:
        """Replace a bucket's tags."""
        tag_set = [{"Key": key, "Value": value} for key, value in tags.items()]
        with aws_error_boundary("s3", "PutBucketTagging"):
            self._client().put_bucket_tagging(Bucket=name, Tagging={"TagSet": tag_set})

    # -- Objects ------------------------------------------------------------------

    def list_objects(
        self: Self,
        bucket: str,
        prefix: str | None,
        delimiter: str | None,
        max_items: int | None,
    ) -> ObjectListing:
        """List objects, fully paginated (up to ``max_items``, or unbounded if ``None``)."""
        kwargs: dict[str, Any] = {"Bucket": bucket}
        if prefix:
            kwargs["Prefix"] = prefix
        if delimiter:
            kwargs["Delimiter"] = delimiter
        pagination_config: dict[str, Any] = {"MaxItems": max_items} if max_items else {}

        objects: list[S3Object] = []
        common_prefixes: list[str] = []
        is_truncated = False
        with aws_error_boundary("s3", "ListObjectsV2"):
            paginator = self._client().get_paginator("list_objects_v2")
            for page in paginator.paginate(PaginationConfig=pagination_config, **kwargs):
                objects.extend(S3Object.model_validate(raw) for raw in page.get("Contents", []))
                common_prefixes.extend(
                    entry["Prefix"] for entry in page.get("CommonPrefixes", [])
                )
                is_truncated = page.get("IsTruncated", False)

        return ObjectListing(
            objects=objects,
            common_prefixes=common_prefixes,
            key_count=len(objects),
            is_truncated=is_truncated,
        )

    def object_exists(self: Self, bucket: str, key: str) -> bool:
        """Return whether ``key`` exists in ``bucket``. A 403 still propagates."""
        try:
            with aws_error_boundary("s3", "HeadObject"):
                self._client().head_object(Bucket=bucket, Key=key)
        except ResourceNotFoundError:
            return False
        return True

    def head_object(self: Self, bucket: str, key: str) -> S3Object:
        """Fetch an object's metadata (no body)."""
        with aws_error_boundary("s3", "HeadObject"):
            response = self._client().head_object(Bucket=bucket, Key=key)
        # HeadObject's response shape differs from a listing's `Contents` entries
        # (`ContentLength` vs `Size`, no `Key` at all) -- normalized here.
        return S3Object.model_validate(
            {
                "Key": key,
                "Size": response["ContentLength"],
                "LastModified": response["LastModified"],
                "ETag": response["ETag"],
                "StorageClass": response.get("StorageClass", "STANDARD"),
            }
        )

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
        """Upload a local file, streaming (multipart when large) -- never loaded whole in memory."""
        extra_args: dict[str, Any] = {}
        if content_type:
            extra_args["ContentType"] = content_type
        if metadata:
            extra_args["Metadata"] = metadata
        if storage_class:
            extra_args["StorageClass"] = storage_class

        with aws_error_boundary("s3", "PutObject"):
            self._client().upload_file(
                Filename=str(source),
                Bucket=bucket,
                Key=key,
                ExtraArgs=extra_args or None,
                Config=_transfer_config(),
                Callback=progress_callback,
            )
        return self.head_object(bucket, key)

    def download_file(
        self: Self,
        bucket: str,
        key: str,
        destination: Path,
        progress_callback: Callable[[int], None] | None,
    ) -> Path:
        """Download an object to a local file, streaming."""
        with aws_error_boundary("s3", "GetObject"):
            self._client().download_file(
                Bucket=bucket,
                Key=key,
                Filename=str(destination),
                Config=_transfer_config(),
                Callback=progress_callback,
            )
        return destination

    def delete_object(self: Self, bucket: str, key: str) -> None:
        """Delete a single object."""
        with aws_error_boundary("s3", "DeleteObject"):
            self._client().delete_object(Bucket=bucket, Key=key)

    def delete_objects(self: Self, bucket: str, keys: Sequence[str]) -> list[str]:
        """Delete up to 1000 objects in one request. Returns the keys actually deleted."""
        if not keys:
            return []
        with aws_error_boundary("s3", "DeleteObjects"):
            response = self._client().delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in keys], "Quiet": False},
            )
        return [item["Key"] for item in response.get("Deleted", [])]

    def copy_object(
        self: Self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
    ) -> S3Object:
        """Copy an object server-side, without downloading/re-uploading its bytes."""
        with aws_error_boundary("s3", "CopyObject"):
            self._client().copy_object(
                Bucket=dst_bucket, Key=dst_key, CopySource={"Bucket": src_bucket, "Key": src_key}
            )
        return self.head_object(dst_bucket, dst_key)

    def generate_presigned_url(
        self: Self, bucket: str, key: str, expires_in: int, method: Literal["get", "put"]
    ) -> str:
        """Generate a presigned URL for a GET or PUT of ``bucket/key``."""
        client_method = "get_object" if method == "get" else "put_object"
        with aws_error_boundary("s3", "GeneratePresignedUrl"):
            url: str = self._client().generate_presigned_url(
                ClientMethod=client_method,
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_in,
            )
        return url


if TYPE_CHECKING:
    # Static conformance check: mypy fails right here if Boto3S3Gateway's method
    # signatures ever drift from the S3Gateway Protocol.
    _s3_gateway_conformance: S3Gateway = cast(Boto3S3Gateway, None)

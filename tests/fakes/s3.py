"""In-memory ``S3Gateway`` test double.

Used instead of ``Mock()`` on purpose -- see ``tests/fakes/iam.py`` for why.
Objects are stored as ``{bucket: {key: (bytes, S3Object)}}`` so tests can
inspect both the raw bytes and the metadata a real upload would produce.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from aws_admin_cli.core.exceptions import ResourceAlreadyExistsError, ResourceNotFoundError
from aws_admin_cli.domain.models.policy import PolicyDocument
from aws_admin_cli.domain.models.s3 import (
    Bucket,
    BucketVersioning,
    ObjectListing,
    S3Object,
    VersioningStatus,
)

_DELETE_BATCH_SIZE = 1000


@dataclass
class FakeS3Gateway:
    """A structurally-typed ``S3Gateway`` double, backed by plain dicts."""

    buckets: dict[str, Bucket] = field(default_factory=dict)
    objects: dict[str, dict[str, bytes]] = field(default_factory=dict)
    object_meta: dict[str, dict[str, S3Object]] = field(default_factory=dict)
    versioning: dict[str, BucketVersioning] = field(default_factory=dict)
    policies: dict[str, PolicyDocument] = field(default_factory=dict)
    tags: dict[str, dict[str, str]] = field(default_factory=dict)
    delete_objects_calls: list[list[str]] = field(default_factory=list)
    block_public_access_calls: dict[str, bool] = field(default_factory=dict)
    encryption: dict[str, str] = field(default_factory=dict)

    # -- Buckets ----------------------------------------------------------------

    def create_bucket(self, name: str, region: str, *, block_public_access: bool) -> Bucket:
        self.block_public_access_calls[name] = block_public_access
        if name in self.buckets:
            raise ResourceAlreadyExistsError(
                f"El bucket '{name}' ya existe.", aws_code="BucketAlreadyExists"
            )
        bucket = Bucket(name=name, creation_date=datetime.now(UTC), region=region)
        self.buckets[name] = bucket
        self.objects[name] = {}
        self.object_meta[name] = {}
        self.versioning[name] = BucketVersioning()
        self.encryption[name] = "AES256"  # SSE-S3, always applied -- mirrors Boto3S3Gateway
        return bucket

    def list_buckets(self) -> list[Bucket]:
        return list(self.buckets.values())

    def bucket_exists(self, name: str) -> bool:
        return name in self.buckets

    def get_bucket_location(self, name: str) -> str:
        self._require_bucket(name)
        return self.buckets[name].region or "us-east-1"

    def delete_bucket(self, name: str) -> None:
        self._require_bucket(name)
        del self.buckets[name]
        self.objects.pop(name, None)
        self.object_meta.pop(name, None)

    def get_bucket_versioning(self, name: str) -> BucketVersioning:
        self._require_bucket(name)
        return self.versioning.get(name, BucketVersioning())

    def get_public_access_block(self, name: str) -> bool:
        self._require_bucket(name)
        return self.block_public_access_calls.get(name, False)

    def set_public_access_block(self, name: str, *, block: bool) -> None:
        self._require_bucket(name)
        self.block_public_access_calls[name] = block

    def get_bucket_encryption(self, name: str) -> str | None:
        self._require_bucket(name)
        return self.encryption.get(name)

    def empty_bucket(self, name: str) -> None:
        self._require_bucket(name)
        self.objects[name] = {}
        self.object_meta[name] = {}

    def set_bucket_versioning(self, name: str, enabled: bool) -> None:
        self._require_bucket(name)
        status = VersioningStatus.ENABLED if enabled else VersioningStatus.SUSPENDED
        self.versioning[name] = BucketVersioning(status=status)

    def get_bucket_policy(self, name: str) -> PolicyDocument | None:
        self._require_bucket(name)
        return self.policies.get(name)

    def set_bucket_policy(self, name: str, document: PolicyDocument) -> None:
        self._require_bucket(name)
        self.policies[name] = document

    def delete_bucket_policy(self, name: str) -> None:
        self._require_bucket(name)
        self.policies.pop(name, None)

    def get_bucket_tags(self, name: str) -> dict[str, str]:
        self._require_bucket(name)
        return dict(self.tags.get(name, {}))

    def set_bucket_tags(self, name: str, tags: dict[str, str]) -> None:
        self._require_bucket(name)
        self.tags[name] = dict(tags)

    def _require_bucket(self, name: str) -> None:
        if name not in self.buckets:
            raise ResourceNotFoundError(f"El bucket '{name}' no existe.", aws_code="NoSuchBucket")

    # -- Objects ------------------------------------------------------------------

    def list_objects(
        self,
        bucket: str,
        prefix: str | None,
        delimiter: str | None,
        max_items: int | None,
    ) -> ObjectListing:
        self._require_bucket(bucket)
        keys = sorted(self.objects.get(bucket, {}))
        if prefix:
            keys = [key for key in keys if key.startswith(prefix)]

        common_prefixes: list[str] = []
        matched_keys: list[str] = []
        if delimiter:
            seen_prefixes: set[str] = set()
            for key in keys:
                remainder = key[len(prefix or "") :]
                if delimiter in remainder:
                    common_prefix = (prefix or "") + remainder.split(delimiter, 1)[0] + delimiter
                    if common_prefix not in seen_prefixes:
                        seen_prefixes.add(common_prefix)
                        common_prefixes.append(common_prefix)
                else:
                    matched_keys.append(key)
        else:
            matched_keys = keys

        if max_items is not None:
            matched_keys = matched_keys[:max_items]

        objects = [self.object_meta[bucket][key] for key in matched_keys]
        return ObjectListing(
            objects=objects, common_prefixes=common_prefixes, key_count=len(objects)
        )

    def object_exists(self, bucket: str, key: str) -> bool:
        return key in self.objects.get(bucket, {})

    def head_object(self, bucket: str, key: str) -> S3Object:
        self._require_bucket(bucket)
        meta = self.object_meta.get(bucket, {}).get(key)
        if meta is None:
            raise ResourceNotFoundError(f"El objeto '{key}' no existe.", aws_code="NoSuchKey")
        return meta

    def upload_file(
        self,
        bucket: str,
        key: str,
        source: Path,
        *,
        content_type: str | None,
        metadata: dict[str, str] | None,
        storage_class: str | None,
        progress_callback: object,
    ) -> S3Object:
        del content_type, metadata, storage_class
        self._require_bucket(bucket)
        data = source.read_bytes()
        if progress_callback is not None:
            progress_callback(len(data))  # type: ignore[operator]
        obj = S3Object.model_validate(
            {
                "Key": key,
                "Size": len(data),
                "LastModified": datetime.now(UTC),
                "ETag": f'"{hashlib.md5(data, usedforsecurity=False).hexdigest()}"',
            }
        )
        self.objects[bucket][key] = data
        self.object_meta[bucket][key] = obj
        return obj

    def download_file(
        self, bucket: str, key: str, destination: Path, progress_callback: object
    ) -> Path:
        data = self.objects.get(bucket, {}).get(key)
        if data is None:
            raise ResourceNotFoundError(f"El objeto '{key}' no existe.", aws_code="NoSuchKey")
        destination.write_bytes(data)
        if progress_callback is not None:
            progress_callback(len(data))  # type: ignore[operator]
        return destination

    def delete_object(self, bucket: str, key: str) -> None:
        self.objects.get(bucket, {}).pop(key, None)
        self.object_meta.get(bucket, {}).pop(key, None)

    def delete_objects(self, bucket: str, keys: object) -> list[str]:
        key_list = list(keys)  # type: ignore[arg-type]
        self.delete_objects_calls.append(key_list)
        deleted: list[str] = []
        for key in key_list:
            if key in self.objects.get(bucket, {}):
                del self.objects[bucket][key]
                self.object_meta[bucket].pop(key, None)
                deleted.append(key)
        return deleted

    def copy_object(self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str) -> S3Object:
        data = self.objects.get(src_bucket, {}).get(src_key)
        if data is None:
            raise ResourceNotFoundError(f"El objeto '{src_key}' no existe.", aws_code="NoSuchKey")
        obj = S3Object.model_validate(
            {
                "Key": dst_key,
                "Size": len(data),
                "LastModified": datetime.now(UTC),
                "ETag": f'"{hashlib.md5(data, usedforsecurity=False).hexdigest()}"',
            }
        )
        self.objects.setdefault(dst_bucket, {})[dst_key] = data
        self.object_meta.setdefault(dst_bucket, {})[dst_key] = obj
        return obj

    def generate_presigned_url(
        self, bucket: str, key: str, expires_in: int, method: Literal["get", "put"]
    ) -> str:
        return f"https://fake-s3.example/{bucket}/{key}?method={method}&expires={expires_in}"

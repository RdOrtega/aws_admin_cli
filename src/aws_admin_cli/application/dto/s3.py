"""Application-layer DTOs for S3 use cases.

Frozen dataclasses (consistent with ``application/dto/iam.py`` and
``ClientFactory``/``AppContext`` elsewhere in the project). The
``progress_callback`` a CLI upload/download passes through is deliberately
NOT part of these DTOs -- it's a presentation-layer concern (a Rich progress
bar), threaded through as its own keyword argument to the use case's
``execute()``, the same way ``S3Gateway`` itself keeps it out of its other
parameters.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aws_admin_cli.domain.models.policy import PolicyDocument


@dataclass(frozen=True, slots=True)
class CreateBucketRequest:
    """Request to create an S3 bucket."""

    name: str
    region: str
    allow_public: bool = False
    enable_versioning: bool = False
    if_not_exists: bool = False


@dataclass(frozen=True, slots=True)
class DeleteBucketRequest:
    """Request to delete an S3 bucket."""

    name: str
    force: bool = False


@dataclass(frozen=True, slots=True)
class SetVersioningRequest:
    """Request to enable or suspend a bucket's versioning."""

    name: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class SetBucketPolicyRequest:
    """Request to replace a bucket's policy."""

    name: str
    document: PolicyDocument
    allow_public: bool = False


@dataclass(frozen=True, slots=True)
class SetBucketTagsRequest:
    """Request to replace a bucket's tags."""

    name: str
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ListObjectsRequest:
    """Request to list a bucket's objects."""

    bucket: str
    prefix: str | None = None
    delimiter: str | None = None
    max_items: int | None = None


@dataclass(frozen=True, slots=True)
class UploadObjectRequest:
    """Request to upload one local file as an S3 object."""

    bucket: str
    key: str
    source: Path
    content_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    storage_class: str | None = None
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class UploadDirectoryRequest:
    """Request to upload a local directory tree, preserving structure as a key prefix."""

    bucket: str
    prefix: str
    source_dir: Path
    exclude: tuple[str, ...] = ()
    storage_class: str | None = None
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class UploadDirectorySummary:
    """Result of an ``upload_directory`` run."""

    uploaded: int
    skipped: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class DownloadObjectRequest:
    """Request to download one S3 object to a local path."""

    bucket: str
    key: str
    destination: Path
    overwrite: bool = False


@dataclass(frozen=True, slots=True)
class DeleteObjectRequest:
    """Request to delete a single object."""

    bucket: str
    key: str


@dataclass(frozen=True, slots=True)
class DeletePrefixRequest:
    """Request to delete every object under a key prefix."""

    bucket: str
    prefix: str


@dataclass(frozen=True, slots=True)
class CopyObjectRequest:
    """Request to copy an object server-side."""

    src_bucket: str
    src_key: str
    dst_bucket: str
    dst_key: str


@dataclass(frozen=True, slots=True)
class PresignUrlRequest:
    """Request to generate a presigned URL."""

    bucket: str
    key: str
    expires_in: int = 3600
    method: Literal["get", "put"] = "get"

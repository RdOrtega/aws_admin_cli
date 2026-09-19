"""Integration tests for Boto3S3Gateway, against moto (no real AWS or LocalStack)."""

import hashlib
from pathlib import Path

import boto3
import pytest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.exceptions import ResourceAlreadyExistsError
from aws_admin_cli.domain.models.policy import PolicyDocument
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.gateways.boto3_s3_gateway import Boto3S3Gateway
from aws_admin_cli.infrastructure.aws.session_factory import Boto3SessionFactory
from moto import mock_aws


@pytest.fixture
def gateway() -> Boto3S3Gateway:
    settings = Settings(profile="testprofile", region="us-east-1")
    client_factory = ClientFactory(
        session_factory=Boto3SessionFactory(profile=settings.profile, region=settings.region),
        settings=settings,
    )
    return Boto3S3Gateway(client_factory=client_factory)


def _scoped_document() -> PolicyDocument:
    return PolicyDocument.model_validate(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "arn:aws:iam::123456789012:root"},
                    "Action": "s3:GetObject",
                    "Resource": "arn:aws:s3:::demo-policy-bucket/*",
                }
            ],
        }
    )


@mock_aws
def test_bucket_full_lifecycle(gateway: Boto3S3Gateway) -> None:
    created = gateway.create_bucket("demo-lifecycle-bucket", "us-east-1", block_public_access=True)
    assert created.name == "demo-lifecycle-bucket"

    assert gateway.bucket_exists("demo-lifecycle-bucket") is True
    assert any(b.name == "demo-lifecycle-bucket" for b in gateway.list_buckets())

    gateway.delete_bucket("demo-lifecycle-bucket")

    assert gateway.bucket_exists("demo-lifecycle-bucket") is False


@mock_aws
def test_create_bucket_in_us_east_1_and_eu_west_1(gateway: Boto3S3Gateway) -> None:
    us_bucket = gateway.create_bucket("demo-us-bucket", "us-east-1", block_public_access=True)
    eu_bucket = gateway.create_bucket("demo-eu-bucket", "eu-west-1", block_public_access=True)

    assert us_bucket.name == "demo-us-bucket"
    assert eu_bucket.name == "demo-eu-bucket"
    assert gateway.get_bucket_location("demo-us-bucket") == "us-east-1"
    assert gateway.get_bucket_location("demo-eu-bucket") == "eu-west-1"


@mock_aws
def test_create_duplicate_bucket_raises_resource_already_exists(gateway: Boto3S3Gateway) -> None:
    # NOTE: us-east-1 is deliberately NOT used here -- S3 has a documented legacy
    # quirk where re-creating your own bucket in us-east-1 succeeds silently (no
    # error) for backward compatibility. Every other region raises
    # BucketAlreadyOwnedByYou even for the same owner, which is what this test
    # is actually meant to exercise.
    gateway.create_bucket("demo-dup-bucket", "eu-west-1", block_public_access=True)

    with pytest.raises(ResourceAlreadyExistsError):
        gateway.create_bucket("demo-dup-bucket", "eu-west-1", block_public_access=True)


@mock_aws
def test_object_upload_download_roundtrip_hashes_match(
    gateway: Boto3S3Gateway, tmp_path: Path
) -> None:
    gateway.create_bucket("demo-object-bucket", "us-east-1", block_public_access=True)
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * (256 * 1024))

    progress_calls: list[int] = []
    obj = gateway.upload_file(
        "demo-object-bucket",
        "a/b/source.bin",
        source,
        content_type=None,
        metadata=None,
        storage_class=None,
        progress_callback=progress_calls.append,
    )
    assert obj.size == source.stat().st_size

    head = gateway.head_object("demo-object-bucket", "a/b/source.bin")
    assert head.size == source.stat().st_size

    destination = tmp_path / "downloaded.bin"
    gateway.download_file("demo-object-bucket", "a/b/source.bin", destination, None)

    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    dest_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
    assert source_hash == dest_hash

    assert progress_calls, "progress_callback should have been invoked at least once"
    assert sum(progress_calls) == source.stat().st_size


@mock_aws
def test_list_objects_is_fully_paginated(gateway: Boto3S3Gateway) -> None:
    gateway.create_bucket("demo-paginated-bucket", "us-east-1", block_public_access=True)
    # Direct client puts (bypassing our gateway) to populate quickly.
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    for i in range(1500):
        client.put_object(Bucket="demo-paginated-bucket", Key=f"key-{i:05d}", Body=b"x")

    listing = gateway.list_objects("demo-paginated-bucket", None, None, None)

    assert len(listing.objects) == 1500
    assert listing.key_count == 1500


@mock_aws
def test_delete_objects_removes_1500_keys(gateway: Boto3S3Gateway) -> None:
    gateway.create_bucket("demo-delete-bucket", "us-east-1", block_public_access=True)
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    keys = [f"key-{i:05d}" for i in range(1500)]
    for key in keys:
        client.put_object(Bucket="demo-delete-bucket", Key=key, Body=b"x")

    deleted: list[str] = []
    for start in range(0, len(keys), 1000):
        deleted.extend(gateway.delete_objects("demo-delete-bucket", keys[start : start + 1000]))

    assert len(deleted) == 1500
    remaining = gateway.list_objects("demo-delete-bucket", None, None, None)
    assert remaining.objects == []


@mock_aws
def test_list_objects_with_delimiter_returns_common_prefixes(gateway: Boto3S3Gateway) -> None:
    gateway.create_bucket("demo-prefix-bucket", "us-east-1", block_public_access=True)
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    client.put_object(Bucket="demo-prefix-bucket", Key="dir1/a.txt", Body=b"x")
    client.put_object(Bucket="demo-prefix-bucket", Key="dir2/b.txt", Body=b"x")
    client.put_object(Bucket="demo-prefix-bucket", Key="root.txt", Body=b"x")

    listing = gateway.list_objects("demo-prefix-bucket", None, "/", None)

    assert set(listing.common_prefixes) == {"dir1/", "dir2/"}
    assert [obj.key for obj in listing.objects] == ["root.txt"]


@mock_aws
def test_bucket_policy_set_get_delete_roundtrip(gateway: Boto3S3Gateway) -> None:
    gateway.create_bucket("demo-policy-bucket", "us-east-1", block_public_access=True)
    document = _scoped_document()

    assert gateway.get_bucket_policy("demo-policy-bucket") is None

    gateway.set_bucket_policy("demo-policy-bucket", document)
    fetched = gateway.get_bucket_policy("demo-policy-bucket")
    assert fetched == document

    gateway.delete_bucket_policy("demo-policy-bucket")
    assert gateway.get_bucket_policy("demo-policy-bucket") is None


@mock_aws
def test_get_missing_bucket_policy_returns_none_not_exception(gateway: Boto3S3Gateway) -> None:
    gateway.create_bucket("demo-no-policy-bucket", "us-east-1", block_public_access=True)

    assert gateway.get_bucket_policy("demo-no-policy-bucket") is None


@mock_aws
def test_versioning_enable_reflects_in_get(gateway: Boto3S3Gateway) -> None:
    gateway.create_bucket("demo-versioned-bucket", "us-east-1", block_public_access=True)

    gateway.set_bucket_versioning("demo-versioned-bucket", True)
    versioning = gateway.get_bucket_versioning("demo-versioned-bucket")

    assert versioning.status.value == "Enabled"


@mock_aws
def test_presigned_url_contains_bucket_key_and_expiration(gateway: Boto3S3Gateway) -> None:
    gateway.create_bucket("demo-presign-bucket", "us-east-1", block_public_access=True)

    url = gateway.generate_presigned_url("demo-presign-bucket", "a/b.txt", 900, "get")

    assert "demo-presign-bucket" in url
    assert "a/b.txt" in url
    assert "Expires" in url or "X-Amz-Expires" in url

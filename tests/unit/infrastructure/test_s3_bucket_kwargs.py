"""Tests for `_create_bucket_kwargs`: S3's us-east-1 CreateBucket API inconsistency.

Isolated from the gateway itself so this needs no network/moto at all.
"""

from aws_admin_cli.infrastructure.aws.gateways.boto3_s3_gateway import _create_bucket_kwargs


def test_us_east_1_omits_create_bucket_configuration() -> None:
    kwargs = _create_bucket_kwargs("my-bucket", "us-east-1")

    assert kwargs == {"Bucket": "my-bucket"}
    assert "CreateBucketConfiguration" not in kwargs


def test_other_region_includes_location_constraint() -> None:
    kwargs = _create_bucket_kwargs("my-bucket", "eu-west-1")

    assert kwargs["Bucket"] == "my-bucket"
    assert kwargs["CreateBucketConfiguration"] == {"LocationConstraint": "eu-west-1"}

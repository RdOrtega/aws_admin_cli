"""Tests for the S3 domain models: bucket-name validation, S3Uri, human_size."""

from datetime import UTC, datetime

import pytest
from aws_admin_cli.core.exceptions import ValidationError as DomainValidationError
from aws_admin_cli.domain.models.policy import PolicyDocument, PolicyEffect, PolicyStatement
from aws_admin_cli.domain.models.s3 import S3Object, S3Uri, validate_bucket_name

# -- validate_bucket_name -------------------------------------------------------

_VALID_NAMES = [
    "my-bucket",
    "a1b",
    "x" * 63,
    "my.bucket.name",
    "bucket123",
    "123bucket",
]

_INVALID_CASES = [
    ("ab", "caracteres"),
    ("x" * 64, "caracteres"),
    ("MyBucket", "minúsculas"),
    ("my_bucket", "minúsculas"),
    ("192.168.1.1", "dirección IP"),
    ("my..bucket", "consecutivos"),
    ("-startdash", "empezar y terminar"),
    ("enddash-", "empezar y terminar"),
    ("xn--test", "prefijo reservado"),
    ("test-s3alias", "sufijo reservado"),
    ("sthree-test", "prefijo reservado"),
    ("amzn-s3-demo-test", "prefijo reservado"),
    ("test--ol-s3", "sufijo reservado"),
    ("test.mrap", "sufijo reservado"),
    (".startdot", "empezar y terminar"),
    ("enddot.", "empezar y terminar"),
    ("has spaces", "minúsculas"),
    ("has/slash", "minúsculas"),
]


@pytest.mark.parametrize("name", _VALID_NAMES)
def test_validate_bucket_name_accepts_valid_names(name: str) -> None:
    assert validate_bucket_name(name) == name


@pytest.mark.parametrize(("name", "expected_fragment"), _INVALID_CASES)
def test_validate_bucket_name_rejects_invalid_names_with_specific_message(
    name: str, expected_fragment: str
) -> None:
    with pytest.raises(DomainValidationError) as exc_info:
        validate_bucket_name(name)

    assert expected_fragment in str(exc_info.value)


def test_validate_bucket_name_warns_but_does_not_raise_on_dots(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="aws_admin_cli"):
        result = validate_bucket_name("my.bucket.name")

    assert result == "my.bucket.name"
    assert any("puntos" in record.getMessage() for record in caplog.records)


# -- S3Uri ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_bucket", "expected_key"),
    [
        ("s3://bucket", "bucket", None),
        ("s3://bucket/", "bucket", None),
        ("s3://bucket/key.txt", "bucket", "key.txt"),
        ("s3://bucket/a/b/c.txt", "bucket", "a/b/c.txt"),
        ("bucket/a/b.txt", "bucket", "a/b.txt"),
        ("bucket", "bucket", None),
        ("s3://bucket/key with spaces.txt", "bucket", "key with spaces.txt"),
        ("s3://bucket/key+special@chars.txt", "bucket", "key+special@chars.txt"),
    ],
    ids=[
        "bucket-only",
        "bucket-only-trailing-slash",
        "bucket-and-key",
        "nested-key",
        "schemeless",
        "schemeless-bucket-only",
        "key-with-spaces",
        "key-with-special-chars",
    ],
)
def test_s3_uri_parse_valid(raw: str, expected_bucket: str, expected_key: str | None) -> None:
    uri = S3Uri.parse(raw)

    assert uri.bucket == expected_bucket
    assert uri.key == expected_key


@pytest.mark.parametrize(
    "raw", ["", "   ", "http://bucket/key", "ftp://bucket", "s3:///key", "/leading-slash"]
)
def test_s3_uri_parse_invalid(raw: str) -> None:
    with pytest.raises(DomainValidationError):
        S3Uri.parse(raw)


def test_s3_uri_str_reconstructs_canonical_uri() -> None:
    assert str(S3Uri.parse("bucket/a/b.txt")) == "s3://bucket/a/b.txt"
    assert str(S3Uri.parse("s3://bucket")) == "s3://bucket"


def test_s3_uri_is_bucket_only() -> None:
    assert S3Uri.parse("s3://bucket").is_bucket_only is True
    assert S3Uri.parse("s3://bucket/key").is_bucket_only is False


# -- human_size -------------------------------------------------------------------


def _object_with_size(size: int) -> S3Object:
    return S3Object.model_validate(
        {
            "Key": "a",
            "Size": size,
            "LastModified": datetime.now(UTC),
            "ETag": '"abc"',
        }
    )


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0.0B"),
        (500, "500.0B"),
        (2048, "2.0KB"),
        (5 * 1024 * 1024, "5.0MB"),
        (3 * 1024 * 1024 * 1024, "3.0GB"),
    ],
    ids=["zero", "bytes", "kilobytes", "megabytes", "gigabytes"],
)
def test_human_size(size: int, expected: str) -> None:
    assert _object_with_size(size).human_size == expected


# -- PolicyDocument still importable from iam.py after the move to policy.py -----


def test_policy_document_still_importable_from_iam_module() -> None:
    import aws_admin_cli.domain.models.iam as iam_module

    assert iam_module.PolicyDocument is PolicyDocument
    assert iam_module.PolicyEffect is PolicyEffect

    document = PolicyDocument(
        statement=[
            PolicyStatement(effect=PolicyEffect.ALLOW, action=["s3:GetObject"], resource=["*"])
        ]
    )
    roundtripped = iam_module.PolicyDocument.from_aws(document.to_aws_json())
    assert roundtripped == document

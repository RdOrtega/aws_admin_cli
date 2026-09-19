"""Tests for the S3 use cases' business rules, using hand-written fakes (no Mock())."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aws_admin_cli.application.dto.s3 import (
    CreateBucketRequest,
    DeleteBucketRequest,
    DeletePrefixRequest,
    DownloadObjectRequest,
    PresignUrlRequest,
    SetBucketPolicyRequest,
    UploadDirectoryRequest,
    UploadObjectRequest,
)
from aws_admin_cli.application.use_cases.s3.audit_buckets import AuditBucketsUseCase
from aws_admin_cli.application.use_cases.s3.create_bucket import CreateBucketUseCase
from aws_admin_cli.application.use_cases.s3.delete_bucket import DeleteBucketUseCase
from aws_admin_cli.application.use_cases.s3.delete_prefix import DeletePrefixUseCase
from aws_admin_cli.application.use_cases.s3.download_object import DownloadObjectUseCase
from aws_admin_cli.application.use_cases.s3.empty_bucket import EmptyBucketUseCase
from aws_admin_cli.application.use_cases.s3.get_bucket_access import GetBucketAccessUseCase
from aws_admin_cli.application.use_cases.s3.list_bucket_access import ListBucketAccessUseCase
from aws_admin_cli.application.use_cases.s3.presign_url import PresignUrlUseCase
from aws_admin_cli.application.use_cases.s3.set_bucket_policy import SetBucketPolicyUseCase
from aws_admin_cli.application.use_cases.s3.set_public_access import SetBucketPublicAccessUseCase
from aws_admin_cli.application.use_cases.s3.upload_directory import UploadDirectoryUseCase
from aws_admin_cli.application.use_cases.s3.upload_object import UploadObjectUseCase
from aws_admin_cli.core.exceptions import AccessDeniedError, ValidationError
from aws_admin_cli.domain.models.policy import PolicyDocument
from aws_admin_cli.domain.models.s3 import S3Object, VersioningStatus

from tests.fakes.iam import InMemoryRepository
from tests.fakes.s3 import FakeS3Gateway

_LOGGER = logging.getLogger("aws_admin_cli")


def _fake_object(key: str) -> S3Object:
    return S3Object.model_validate(
        {"Key": key, "Size": 4, "LastModified": datetime.now(UTC), "ETag": '"x"'}
    )


def _public_document() -> PolicyDocument:
    return PolicyDocument.model_validate(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": "*"}
            ],
        }
    )


def _scoped_document() -> PolicyDocument:
    return PolicyDocument.model_validate(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "arn:aws:iam::123456789012:root"},
                    "Action": "s3:GetObject",
                    "Resource": "*",
                }
            ],
        }
    )


# -- create_bucket --------------------------------------------------------------


def test_create_bucket_applies_block_public_access_by_default() -> None:
    gateway = FakeS3Gateway()
    use_case = CreateBucketUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", logger=_LOGGER
    )

    use_case.execute(CreateBucketRequest(name="my-bucket", region="us-east-1"))

    assert gateway.block_public_access_calls["my-bucket"] is True


def test_create_bucket_enables_sse_s3_encryption_by_default() -> None:
    gateway = FakeS3Gateway()
    use_case = CreateBucketUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", logger=_LOGGER
    )

    use_case.execute(CreateBucketRequest(name="my-bucket", region="us-east-1"))

    assert gateway.get_bucket_encryption("my-bucket") == "AES256"


def test_create_bucket_with_allow_public_does_not_apply_it_and_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gateway = FakeS3Gateway()
    use_case = CreateBucketUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", logger=_LOGGER
    )

    with caplog.at_level("WARNING", logger="aws_admin_cli"):
        use_case.execute(
            CreateBucketRequest(name="my-bucket", region="us-east-1", allow_public=True)
        )

    assert gateway.block_public_access_calls["my-bucket"] is False
    assert any("Block Public Access" in record.getMessage() for record in caplog.records)


def test_create_bucket_if_not_exists_over_existing_does_not_raise() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    use_case = CreateBucketUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", logger=_LOGGER
    )

    bucket = use_case.execute(
        CreateBucketRequest(name="my-bucket", region="us-east-1", if_not_exists=True)
    )

    assert bucket.name == "my-bucket"


# -- delete_bucket ----------------------------------------------------------------


def test_delete_bucket_with_objects_and_no_force_raises_and_bucket_still_exists() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    gateway.objects["my-bucket"]["a.txt"] = b"data"
    gateway.object_meta["my-bucket"]["a.txt"] = _fake_object("a.txt")
    use_case = DeleteBucketUseCase(gateway=gateway, repository=InMemoryRepository())

    with pytest.raises(ValidationError):
        use_case.execute(DeleteBucketRequest(name="my-bucket"))

    assert gateway.bucket_exists("my-bucket")


def test_delete_bucket_with_force_over_2500_objects_batches_in_three_calls() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    for i in range(2500):
        key = f"key-{i:05d}"
        gateway.objects["my-bucket"][key] = b"x"
        gateway.object_meta["my-bucket"][key] = _fake_object(key)
    use_case = DeleteBucketUseCase(gateway=gateway, repository=InMemoryRepository())

    use_case.execute(DeleteBucketRequest(name="my-bucket", force=True))

    assert len(gateway.delete_objects_calls) == 3
    assert [len(batch) for batch in gateway.delete_objects_calls] == [1000, 1000, 500]
    assert not gateway.bucket_exists("my-bucket")


# -- upload_object / download_object ---------------------------------------------


def test_upload_object_existing_key_without_overwrite_raises(tmp_path: Path) -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    source = tmp_path / "file.txt"
    source.write_text("hello")
    use_case = UploadObjectUseCase(
        gateway=gateway,
        repository=InMemoryRepository(),
        profile="localstack",
        region="us-east-1",
    )
    use_case.execute(UploadObjectRequest(bucket="my-bucket", key="file.txt", source=source))

    with pytest.raises(ValidationError):
        use_case.execute(UploadObjectRequest(bucket="my-bucket", key="file.txt", source=source))


@pytest.mark.parametrize(
    ("filename", "expected_prefix"),
    [("data.json", "application/json"), ("image.png", "image/png")],
)
def test_upload_object_detects_content_type(
    tmp_path: Path, filename: str, expected_prefix: str
) -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    source = tmp_path / filename
    source.write_bytes(b"data")
    use_case = UploadObjectUseCase(
        gateway=gateway,
        repository=InMemoryRepository(),
        profile="localstack",
        region="us-east-1",
    )
    captured: dict[str, str | None] = {}
    original_upload_file = gateway.upload_file

    def spy_upload_file(*args: object, **kwargs: object) -> object:
        captured["content_type"] = kwargs.get("content_type")  # type: ignore[assignment]
        return original_upload_file(*args, **kwargs)  # type: ignore[arg-type]

    gateway.upload_file = spy_upload_file  # type: ignore[method-assign]

    use_case.execute(UploadObjectRequest(bucket="my-bucket", key=filename, source=source))

    assert captured["content_type"] == expected_prefix


def test_download_object_existing_destination_without_overwrite_raises(tmp_path: Path) -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    source = tmp_path / "file.txt"
    source.write_text("hello")
    upload_use_case = UploadObjectUseCase(
        gateway=gateway,
        repository=InMemoryRepository(),
        profile="localstack",
        region="us-east-1",
    )
    upload_use_case.execute(UploadObjectRequest(bucket="my-bucket", key="file.txt", source=source))

    destination = tmp_path / "downloaded.txt"
    destination.write_text("already here")
    download_use_case = DownloadObjectUseCase(gateway=gateway)

    with pytest.raises(ValidationError):
        download_use_case.execute(
            DownloadObjectRequest(bucket="my-bucket", key="file.txt", destination=destination)
        )


# -- set_bucket_policy --------------------------------------------------------------


def test_set_bucket_policy_with_public_principal_and_no_flag_raises() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    use_case = SetBucketPolicyUseCase(gateway=gateway)

    with pytest.raises(ValidationError):
        use_case.execute(SetBucketPolicyRequest(name="my-bucket", document=_public_document()))


def test_set_bucket_policy_with_public_principal_and_allow_flag_succeeds() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    use_case = SetBucketPolicyUseCase(gateway=gateway)

    use_case.execute(
        SetBucketPolicyRequest(name="my-bucket", document=_public_document(), allow_public=True)
    )

    assert gateway.policies["my-bucket"] == _public_document()


def test_set_bucket_policy_with_scoped_principal_succeeds_without_flag() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    use_case = SetBucketPolicyUseCase(gateway=gateway)

    use_case.execute(SetBucketPolicyRequest(name="my-bucket", document=_scoped_document()))

    assert gateway.policies["my-bucket"] == _scoped_document()


# -- presign_url --------------------------------------------------------------------


@pytest.mark.parametrize("expires_in", [0, -1, 604801])
def test_presign_out_of_range_raises(expires_in: int) -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    use_case = PresignUrlUseCase(gateway=gateway, logger=_LOGGER)

    with pytest.raises(ValidationError):
        use_case.execute(PresignUrlRequest(bucket="my-bucket", key="a.txt", expires_in=expires_in))


def test_presign_at_max_boundary_succeeds() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    use_case = PresignUrlUseCase(gateway=gateway, logger=_LOGGER)

    url = use_case.execute(PresignUrlRequest(bucket="my-bucket", key="a.txt", expires_in=604800))

    assert "my-bucket" in url


def test_presign_over_one_day_succeeds_but_warns(caplog: pytest.LogCaptureFixture) -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    use_case = PresignUrlUseCase(gateway=gateway, logger=_LOGGER)

    with caplog.at_level("WARNING", logger="aws_admin_cli"):
        url = use_case.execute(PresignUrlRequest(bucket="my-bucket", key="a.txt", expires_in=90000))

    assert "my-bucket" in url
    assert any("vida" in record.getMessage() for record in caplog.records)


# -- delete_prefix --------------------------------------------------------------------


def test_delete_prefix_with_no_matches_returns_zero_without_raising() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    use_case = DeletePrefixUseCase(gateway=gateway, repository=InMemoryRepository())

    count = use_case.execute(DeletePrefixRequest(bucket="my-bucket", prefix="nope/"))

    assert count == 0


# -- upload_directory ------------------------------------------------------------


def _upload_directory_use_case(gateway: FakeS3Gateway) -> UploadDirectoryUseCase:
    upload_object = UploadObjectUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", region="us-east-1"
    )
    return UploadDirectoryUseCase(upload_object=upload_object)


def test_upload_directory_raises_when_source_is_not_a_directory(tmp_path: Path) -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    use_case = _upload_directory_use_case(gateway)
    missing = tmp_path / "does-not-exist"

    with pytest.raises(ValidationError):
        use_case.execute(
            UploadDirectoryRequest(bucket="my-bucket", prefix="", source_dir=missing)
        )


def test_upload_directory_uploads_recursively_preserving_prefix_structure(
    tmp_path: Path,
) -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "nested" / "b.txt").write_bytes(b"world!")
    use_case = _upload_directory_use_case(gateway)

    summary = use_case.execute(
        UploadDirectoryRequest(bucket="my-bucket", prefix="uploads", source_dir=tmp_path)
    )

    assert summary.uploaded == 2
    assert summary.skipped == 0
    assert summary.total_bytes == len(b"hello") + len(b"world!")
    assert set(gateway.objects["my-bucket"]) == {"uploads/a.txt", "uploads/nested/b.txt"}


def test_upload_directory_skips_files_matching_exclude_patterns(tmp_path: Path) -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    (tmp_path / "keep.txt").write_bytes(b"keep")
    (tmp_path / "skip.log").write_bytes(b"skip-me")
    use_case = _upload_directory_use_case(gateway)

    summary = use_case.execute(
        UploadDirectoryRequest(
            bucket="my-bucket", prefix="", source_dir=tmp_path, exclude=("*.log",)
        )
    )

    assert summary.uploaded == 1
    assert summary.skipped == 1
    assert set(gateway.objects["my-bucket"]) == {"keep.txt"}


def test_upload_directory_with_empty_prefix_uses_relative_path_as_key(tmp_path: Path) -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    (tmp_path / "root.txt").write_bytes(b"x")
    use_case = _upload_directory_use_case(gateway)

    use_case.execute(UploadDirectoryRequest(bucket="my-bucket", prefix="", source_dir=tmp_path))

    assert "root.txt" in gateway.objects["my-bucket"]


# -- empty_bucket -----------------------------------------------------------------


def test_empty_bucket_removes_all_objects_but_keeps_the_bucket() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("my-bucket", "us-east-1", block_public_access=True)
    gateway.objects["my-bucket"]["a.txt"] = b"hi"
    gateway.object_meta["my-bucket"]["a.txt"] = _fake_object("a.txt")

    EmptyBucketUseCase(gateway=gateway).execute("my-bucket")

    assert gateway.objects["my-bucket"] == {}
    assert "my-bucket" in gateway.buckets


# -- audit_buckets ------------------------------------------------------------------


def test_audit_buckets_reports_public_access_encryption_versioning_and_size() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("safe-bucket", "us-east-1", block_public_access=True)
    gateway.create_bucket("risky-bucket", "us-east-1", block_public_access=False)
    gateway.encryption.pop("risky-bucket")  # simulate an unencrypted bucket
    gateway.set_bucket_versioning("safe-bucket", True)
    gateway.objects["safe-bucket"]["a.txt"] = b"1234"
    gateway.object_meta["safe-bucket"]["a.txt"] = _fake_object("a.txt")

    entries = {entry.name: entry for entry in AuditBucketsUseCase(gateway=gateway).execute()}

    safe = entries["safe-bucket"]
    assert safe.public_access_blocked is True
    assert safe.encryption == "AES256"
    assert safe.versioning is VersioningStatus.ENABLED
    assert safe.object_count == 1
    assert safe.total_size == 4

    risky = entries["risky-bucket"]
    assert risky.public_access_blocked is False
    assert risky.encryption is None
    assert risky.versioning is VersioningStatus.DISABLED


# -- get_bucket_access / list_bucket_access ------------------------------------------


def test_get_bucket_access_is_private_when_block_public_access_is_fully_on() -> None:
    """Block Public Access fully enabled settles it -- even a public policy statement
    underneath it doesn't make the bucket actually public.
    """
    gateway = FakeS3Gateway()
    gateway.create_bucket("locked-down", "us-east-1", block_public_access=True)
    gateway.set_bucket_policy("locked-down", _public_document())

    assert GetBucketAccessUseCase(gateway=gateway).execute("locked-down") is False


def test_get_bucket_access_is_public_when_block_public_access_is_off() -> None:
    """Block Public Access off (any of the 4 flags False) is Public immediately --
    a bucket created with ``--allow-public`` needs no separate policy grant to be
    counted as Public, matching ``AuditBucketsUseCase``'s ``public_access_blocked``.
    """
    gateway = FakeS3Gateway()
    gateway.create_bucket("open-bpa-no-policy", "us-east-1", block_public_access=False)

    assert GetBucketAccessUseCase(gateway=gateway).execute("open-bpa-no-policy") is True


def test_get_bucket_access_is_public_regardless_of_the_bucket_policy_when_bpa_is_off() -> None:
    """A scoped, non-public policy underneath an off Block Public Access still reads
    as Public -- the policy is no longer consulted at all.
    """
    gateway = FakeS3Gateway()
    gateway.create_bucket("scoped-policy", "us-east-1", block_public_access=False)
    gateway.set_bucket_policy("scoped-policy", _scoped_document())

    assert GetBucketAccessUseCase(gateway=gateway).execute("scoped-policy") is True


def test_get_bucket_access_is_public_with_no_public_access_block_configuration_at_all() -> None:
    """``NoSuchPublicAccessBlockConfiguration`` (``FakeS3Gateway``'s default for a
    bucket that never had one set) reads as Public, same as any flag off.
    """
    gateway = FakeS3Gateway()
    gateway.create_bucket("never-configured", "us-east-1", block_public_access=False)
    del gateway.block_public_access_calls["never-configured"]

    assert GetBucketAccessUseCase(gateway=gateway).execute("never-configured") is True


def test_get_bucket_access_defaults_to_private_on_an_aws_error() -> None:
    """A permissions gap (or any other AWS error) reading Block Public Access must
    never crash this display badge -- it defaults safely to Private instead.
    """
    gateway = FakeS3Gateway()
    gateway.create_bucket("mystery-bucket", "us-east-1", block_public_access=False)

    def _raise(name: str) -> bool:
        raise AccessDeniedError("nope", aws_code="AccessDenied")

    gateway.get_public_access_block = _raise  # type: ignore[method-assign]

    assert GetBucketAccessUseCase(gateway=gateway).execute("mystery-bucket") is False


def test_list_bucket_access_resolves_public_private_for_every_bucket_given() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("public-one", "us-east-1", block_public_access=False)
    gateway.create_bucket("private-one", "us-east-1", block_public_access=True)

    buckets = gateway.list_buckets()
    result = ListBucketAccessUseCase(
        get_bucket_access=GetBucketAccessUseCase(gateway=gateway)
    ).execute(buckets)

    assert result == {"public-one": True, "private-one": False}


# -- set_public_access (Bucket Detail's Toggle Public Access Block action) ----------


def test_set_public_access_with_block_true_enables_all_4_flags() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("exposed", "us-east-1", block_public_access=False)

    SetBucketPublicAccessUseCase(gateway=gateway).execute("exposed", block=True)

    assert gateway.get_public_access_block("exposed") is True


def test_set_public_access_with_block_false_disables_all_4_flags() -> None:
    gateway = FakeS3Gateway()
    gateway.create_bucket("locked-down", "us-east-1", block_public_access=True)

    SetBucketPublicAccessUseCase(gateway=gateway).execute("locked-down", block=False)

    assert gateway.get_public_access_block("locked-down") is False

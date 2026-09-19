"""Tests for AmiResolver: ami-id / alias / exact-name resolution, and caching."""

from datetime import UTC, datetime

import pytest
from aws_admin_cli.application.services.ami_resolver import AmiResolver
from aws_admin_cli.core.exceptions import ResourceNotFoundError
from aws_admin_cli.domain.models.ec2 import Ami

from tests.fakes.ec2 import FakeEc2Gateway

_AMI_ID = "ami-0123456789abcdef0"


def _ami(image_id: str, name: str, owner_id: str, creation_date: datetime) -> Ami:
    return Ami(
        image_id=image_id,
        name=name,
        owner_id=owner_id,
        creation_date=creation_date,
        architecture="x86_64",
    )


# -- by ami-id --------------------------------------------------------------------


def test_resolve_by_ami_id() -> None:
    known_ami = _ami(_AMI_ID, "some-custom-ami", "111111111111", datetime(2024, 1, 1, tzinfo=UTC))
    gateway = FakeEc2Gateway(amis={_AMI_ID: known_ami})
    resolver = AmiResolver(gateway=gateway)

    ami = resolver.resolve(_AMI_ID)

    assert ami.image_id == _AMI_ID


def test_resolve_by_ami_id_not_found_raises() -> None:
    resolver = AmiResolver(gateway=FakeEc2Gateway())
    with pytest.raises(ResourceNotFoundError):
        resolver.resolve("ami-0000000000000000")


# -- by alias -----------------------------------------------------------------------


def test_resolve_by_alias_returns_the_newest_candidate() -> None:
    older = _ami(
        "ami-1111111111111111",
        "al2023-ami-2023.1.20240101-x86_64",
        "amazon",
        datetime(2024, 1, 1, tzinfo=UTC),
    )
    newer = _ami(
        "ami-2222222222222222",
        "al2023-ami-2023.2.20240601-x86_64",
        "amazon",
        datetime(2024, 6, 1, tzinfo=UTC),
    )
    gateway = FakeEc2Gateway(amis={older.image_id: older, newer.image_id: newer})
    resolver = AmiResolver(gateway=gateway)

    resolved = resolver.resolve("amazon-linux-2023")

    assert resolved.image_id == newer.image_id


def test_resolve_by_alias_with_no_candidates_raises_with_alias_hint() -> None:
    resolver = AmiResolver(gateway=FakeEc2Gateway())
    with pytest.raises(ResourceNotFoundError) as exc_info:
        resolver.resolve("ubuntu-22.04")
    assert "ami-id" in str(exc_info.value.hint)


# -- by exact name / unknown alias --------------------------------------------------


def test_resolve_by_exact_name() -> None:
    ami = _ami(
        "ami-3333333333333333",
        "my-custom-golden-image",
        "111111111111",
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    gateway = FakeEc2Gateway(amis={ami.image_id: ami})
    resolver = AmiResolver(gateway=gateway)

    resolved = resolver.resolve("my-custom-golden-image")

    assert resolved.image_id == ami.image_id


def test_resolve_unknown_reference_lists_known_aliases_in_the_hint() -> None:
    resolver = AmiResolver(gateway=FakeEc2Gateway())
    with pytest.raises(ResourceNotFoundError) as exc_info:
        resolver.resolve("no-existe-ni-como-alias-ni-como-nombre")
    hint = str(exc_info.value.hint)
    assert "amazon-linux-2023" in hint
    assert "ubuntu-22.04" in hint


# -- caching ------------------------------------------------------------------------


def test_resolving_the_same_reference_twice_costs_one_describe_images_call() -> None:
    ami = _ami(_AMI_ID, "some-custom-ami", "111111111111", datetime(2024, 1, 1, tzinfo=UTC))
    gateway = FakeEc2Gateway(amis={_AMI_ID: ami})
    resolver = AmiResolver(gateway=gateway)

    resolver.resolve(_AMI_ID)
    resolver.resolve(_AMI_ID)

    assert gateway.call_counts["describe_images"] == 1

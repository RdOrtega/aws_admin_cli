"""Tests for LaunchInstanceUseCase -- the most important use case in this project.

Exercises resolution (AMI alias, subnet/SG by name), idempotency via
ClientToken, dry-run, the launch guard rails, --iam-role wiring, mandatory
tags, and ResourceRecord tracking -- all through fakes, no boto3/moto.
"""

import logging
from datetime import UTC, datetime

import pytest
from aws_admin_cli.application.dto.ec2 import LaunchInstanceRequest
from aws_admin_cli.application.dto.iam import CreateRoleRequest
from aws_admin_cli.application.services.ami_resolver import AmiResolver
from aws_admin_cli.application.services.network_resolver import NetworkResolver
from aws_admin_cli.application.use_cases.ec2.launch_instance import LaunchInstanceUseCase
from aws_admin_cli.application.use_cases.iam.create_role import CreateRoleUseCase
from aws_admin_cli.application.use_cases.iam.ensure_instance_profile_for_role import (
    EnsureInstanceProfileForRoleUseCase,
)
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.ec2 import Ami
from aws_admin_cli.domain.models.iam import service_trust_policy

from tests.fakes.ec2 import FakeEc2Gateway
from tests.fakes.iam import FakeIamGateway, InMemoryRepository
from tests.fakes.vpc import FakeVpcGateway

_PROFILE = "localstack"
_REGION = "us-east-1"
_ALLOWED_INSTANCE_TYPE = "t3.micro"
_DISALLOWED_INSTANCE_TYPE = "c5.xlarge"  # family "c5" is not in DEFAULT_ALLOWED_FAMILIES


def _alias_ami(image_id: str, suffix: str, when: datetime) -> Ami:
    return Ami(
        image_id=image_id,
        name=f"al2023-ami-2023.{suffix}-x86_64",
        owner_id="amazon",
        creation_date=when,
        architecture="x86_64",
    )


def _base_request(**overrides: object) -> LaunchInstanceRequest:
    defaults: dict[str, object] = {
        "name": "demo-web",
        "ami_ref": "amazon-linux-2023",
        "instance_type": _ALLOWED_INSTANCE_TYPE,
        "subnet_ref": "corp-private-1a",
        "security_group_refs": ("corp-web-sg",),
    }
    defaults.update(overrides)
    return LaunchInstanceRequest(**defaults)


def _make_use_case(
    ec2_gateway: FakeEc2Gateway | None = None,
    iam_gateway: FakeIamGateway | None = None,
    repository: InMemoryRepository | None = None,
) -> tuple[LaunchInstanceUseCase, FakeEc2Gateway, InMemoryRepository]:
    ec2_gateway = ec2_gateway if ec2_gateway is not None else FakeEc2Gateway()
    iam_gateway = iam_gateway if iam_gateway is not None else FakeIamGateway()
    repository = repository if repository is not None else InMemoryRepository()

    ami_a = _alias_ami("ami-1111111111111111", "1.20240101", datetime(2024, 1, 1, tzinfo=UTC))
    ami_b = _alias_ami("ami-2222222222222222", "2.20240601", datetime(2024, 6, 1, tzinfo=UTC))
    ec2_gateway.amis[ami_a.image_id] = ami_a
    ec2_gateway.amis[ami_b.image_id] = ami_b

    ensure_instance_profile = EnsureInstanceProfileForRoleUseCase(
        gateway=iam_gateway, repository=repository, profile=_PROFILE, region=_REGION
    )
    use_case = LaunchInstanceUseCase(
        gateway=ec2_gateway,
        ami_resolver=AmiResolver(gateway=ec2_gateway),
        network_resolver=NetworkResolver(gateway=FakeVpcGateway()),
        ensure_instance_profile=ensure_instance_profile,
        repository=repository,
        profile=_PROFILE,
        region=_REGION,
        logger=logging.getLogger("test.launch_instance"),
    )
    return use_case, ec2_gateway, repository


# -- happy path -----------------------------------------------------------------


def test_launch_resolves_ami_alias_subnet_and_sg_by_name() -> None:
    use_case, ec2_gateway, _repository = _make_use_case()

    instance = use_case.execute(_base_request())

    assert instance.image_id == "ami-2222222222222222"  # newest AL2023 candidate
    assert instance.subnet_id == "subnet-0a1b2c03"  # corp-private-1a
    assert instance.security_group_ids == ["sg-0a1b2c01"]  # corp-web-sg
    assert instance.instance_id in ec2_gateway.instances


# -- idempotency ------------------------------------------------------------------


def test_repeating_the_same_request_produces_one_instance() -> None:
    use_case, ec2_gateway, _repository = _make_use_case()
    request = _base_request(client_token_nonce="fixed-nonce")

    first = use_case.execute(request)
    second = use_case.execute(request)

    assert first.instance_id == second.instance_id
    assert len(ec2_gateway.instances) == 1


def test_different_client_tokens_produce_different_instances() -> None:
    use_case, ec2_gateway, _repository = _make_use_case()

    first = use_case.execute(_base_request(client_token_nonce="a"))
    second = use_case.execute(_base_request(client_token_nonce="b"))

    assert first.instance_id != second.instance_id
    assert len(ec2_gateway.instances) == 2


# -- dry-run ------------------------------------------------------------------------


def test_dry_run_does_not_create_an_instance_or_a_resource_record() -> None:
    use_case, ec2_gateway, repository = _make_use_case()

    instance = use_case.execute(_base_request(dry_run=True))

    assert instance.instance_id == "dry-run"
    assert not ec2_gateway.instances
    assert not repository.list_all()


# -- guard rails --------------------------------------------------------------------


def test_disallowed_instance_type_family_without_confirm_large_raises() -> None:
    use_case, _ec2_gateway, _repository = _make_use_case()

    with pytest.raises(ValidationError) as exc_info:
        use_case.execute(_base_request(instance_type=_DISALLOWED_INSTANCE_TYPE))

    assert "c5" in str(exc_info.value)


def test_disallowed_instance_type_family_with_confirm_large_succeeds() -> None:
    use_case, _ec2_gateway, _repository = _make_use_case()

    instance = use_case.execute(
        _base_request(instance_type=_DISALLOWED_INSTANCE_TYPE, confirm_large=True)
    )

    assert instance.instance_type == _DISALLOWED_INSTANCE_TYPE


def test_user_data_with_aws_access_key_raises() -> None:
    use_case, _ec2_gateway, _repository = _make_use_case()

    with pytest.raises(ValidationError):
        use_case.execute(
            _base_request(user_data="export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")
        )


def test_oversized_user_data_raises() -> None:
    use_case, _ec2_gateway, _repository = _make_use_case()

    with pytest.raises(ValidationError):
        use_case.execute(_base_request(user_data="x" * 20_000))


# -- --iam-role -----------------------------------------------------------------------


def test_iam_role_ensures_instance_profile_and_wires_its_arn() -> None:
    iam_gateway = FakeIamGateway()
    repository = InMemoryRepository()
    CreateRoleUseCase(
        gateway=iam_gateway, repository=repository, profile=_PROFILE, region=_REGION
    ).execute(
        CreateRoleRequest(
            name="demo-ec2-role", trust_policy=service_trust_policy("ec2.amazonaws.com")
        )
    )
    use_case, _ec2_gateway, _repo = _make_use_case(iam_gateway=iam_gateway, repository=repository)

    instance = use_case.execute(_base_request(iam_role="demo-ec2-role"))

    profile = iam_gateway.get_instance_profile("demo-ec2-role")
    assert instance.iam_instance_profile_arn == profile.arn


def test_iam_instance_profile_arn_wires_directly_without_ensuring_a_profile() -> None:
    use_case, _ec2_gateway, _repository = _make_use_case()
    prebuilt_arn = "arn:aws:iam::123456789012:instance-profile/prebuilt-profile"

    instance = use_case.execute(_base_request(iam_instance_profile_arn=prebuilt_arn))

    assert instance.iam_instance_profile_arn == prebuilt_arn


def test_iam_role_and_iam_instance_profile_arn_together_raises() -> None:
    use_case, _ec2_gateway, _repository = _make_use_case()

    with pytest.raises(ValidationError) as exc_info:
        use_case.execute(
            _base_request(
                iam_role="demo-ec2-role",
                iam_instance_profile_arn="arn:aws:iam::123456789012:instance-profile/x",
            )
        )

    assert "iam_role" in str(exc_info.value)


# -- mandatory tags -------------------------------------------------------------------


def test_mandatory_tags_are_always_present() -> None:
    use_case, _ec2_gateway, _repository = _make_use_case()

    instance = use_case.execute(_base_request(tags={"Owner": "ruben"}))

    tags = {tag["Key"]: tag["Value"] for tag in instance.tags}
    assert tags["ManagedBy"] == "aws-admin-cli"
    assert tags["Name"] == "demo-web"
    assert "CreatedAt" in tags
    assert tags["Owner"] == "ruben"


# -- unresolvable security group -------------------------------------------------------


def test_unknown_security_group_raises_resource_not_found_with_hint() -> None:
    use_case, ec2_gateway, _repository = _make_use_case()

    with pytest.raises(ResourceNotFoundError) as exc_info:
        use_case.execute(_base_request(security_group_refs=("no-existe-este-sg",)))

    assert exc_info.value.hint is not None
    assert not ec2_gateway.instances


# -- ResourceRecord tracking -----------------------------------------------------------


def test_successful_launch_writes_a_rich_resource_record() -> None:
    use_case, _ec2_gateway, repository = _make_use_case()

    instance = use_case.execute(_base_request())

    record = repository.get(f"ec2:instance:{instance.instance_id}")
    assert record is not None
    assert record.metadata["ami"] == instance.image_id
    assert record.metadata["instance_type"] == _ALLOWED_INSTANCE_TYPE
    assert record.metadata["subnet_id"] == instance.subnet_id
    assert record.metadata["security_group_ids"] == instance.security_group_ids
    assert record.metadata["name"] == "demo-web"

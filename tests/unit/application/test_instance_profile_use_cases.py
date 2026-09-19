"""Tests for the IAM instance-profile use cases, using FakeIamGateway (no Mock())."""

import pytest
from aws_admin_cli.application.dto.iam import (
    AttachRoleToProfileRequest,
    CreateInstanceProfileRequest,
    CreateRoleRequest,
    DeleteInstanceProfileRequest,
)
from aws_admin_cli.application.use_cases.iam.attach_role_to_profile import (
    AttachRoleToProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.create_instance_profile import (
    CreateInstanceProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.create_role import CreateRoleUseCase
from aws_admin_cli.application.use_cases.iam.delete_instance_profile import (
    DeleteInstanceProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.ensure_instance_profile_for_role import (
    EnsureInstanceProfileForRoleUseCase,
)
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.iam import service_trust_policy

from tests.fakes.iam import FakeIamGateway, InMemoryRepository

_PROFILE = "localstack"
_REGION = "us-east-1"


def _create_role(gateway: FakeIamGateway, name: str = "demo-ec2-role") -> None:
    CreateRoleUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile=_PROFILE, region=_REGION
    ).execute(CreateRoleRequest(name=name, trust_policy=service_trust_policy("ec2.amazonaws.com")))


# -- create_instance_profile ----------------------------------------------------


def test_create_instance_profile_writes_resource_record() -> None:
    gateway = FakeIamGateway()
    repository = InMemoryRepository()
    use_case = CreateInstanceProfileUseCase(
        gateway=gateway, repository=repository, profile=_PROFILE, region=_REGION
    )

    profile = use_case.execute(CreateInstanceProfileRequest(name="demo-profile"))

    assert repository.get("iam:instance-profile:demo-profile") is not None
    assert profile.instance_profile_name == "demo-profile"


def test_create_instance_profile_if_not_exists_returns_existing_without_error() -> None:
    gateway = FakeIamGateway()
    use_case = CreateInstanceProfileUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile=_PROFILE, region=_REGION
    )
    first = use_case.execute(CreateInstanceProfileRequest(name="demo-profile"))

    second = use_case.execute(CreateInstanceProfileRequest(name="demo-profile", if_not_exists=True))

    assert second.arn == first.arn


# -- attach_role_to_profile ------------------------------------------------------


def test_attach_role_to_profile_is_idempotent() -> None:
    gateway = FakeIamGateway()
    _create_role(gateway)
    gateway.create_instance_profile("demo-profile", "/")
    use_case = AttachRoleToProfileUseCase(gateway=gateway)
    request = AttachRoleToProfileRequest(profile_name="demo-profile", role_name="demo-ec2-role")

    first = use_case.execute(request)
    second = use_case.execute(request)

    assert first.role_name == "demo-ec2-role"
    assert second.role_name == "demo-ec2-role"


# -- delete_instance_profile ------------------------------------------------------


def test_delete_instance_profile_with_attached_role_and_no_force_raises() -> None:
    gateway = FakeIamGateway()
    repository = InMemoryRepository()
    _create_role(gateway)
    gateway.create_instance_profile("demo-profile", "/")
    gateway.add_role_to_instance_profile("demo-profile", "demo-ec2-role")
    use_case = DeleteInstanceProfileUseCase(gateway=gateway, repository=repository)

    with pytest.raises(ValidationError):
        use_case.execute(DeleteInstanceProfileRequest(name="demo-profile"))

    assert "demo-profile" in gateway.instance_profiles


def test_delete_instance_profile_with_force_detaches_and_deletes() -> None:
    gateway = FakeIamGateway()
    repository = InMemoryRepository()
    _create_role(gateway)
    gateway.create_instance_profile("demo-profile", "/")
    gateway.add_role_to_instance_profile("demo-profile", "demo-ec2-role")
    use_case = DeleteInstanceProfileUseCase(gateway=gateway, repository=repository)

    use_case.execute(DeleteInstanceProfileRequest(name="demo-profile", force=True))

    assert "demo-profile" not in gateway.instance_profiles


# -- ensure_instance_profile_for_role (THE important composed use case) ----------


def test_ensure_instance_profile_for_role_creates_what_is_missing() -> None:
    gateway = FakeIamGateway()
    repository = InMemoryRepository()
    _create_role(gateway)
    use_case = EnsureInstanceProfileForRoleUseCase(
        gateway=gateway, repository=repository, profile=_PROFILE, region=_REGION
    )

    arn = use_case.execute("demo-ec2-role")

    profile = gateway.get_instance_profile("demo-ec2-role")
    assert arn == profile.arn
    assert profile.role_name == "demo-ec2-role"


def test_ensure_instance_profile_for_role_is_idempotent_across_two_calls() -> None:
    gateway = FakeIamGateway()
    repository = InMemoryRepository()
    _create_role(gateway)
    use_case = EnsureInstanceProfileForRoleUseCase(
        gateway=gateway, repository=repository, profile=_PROFILE, region=_REGION
    )

    first_arn = use_case.execute("demo-ec2-role")
    second_arn = use_case.execute("demo-ec2-role")

    assert first_arn == second_arn
    # No duplicate ResourceRecord and no second profile created.
    assert len(gateway.instance_profiles) == 1
    assert (
        len(
            [item for item in repository.list_all() if item.resource_type == "iam:instance-profile"]
        )
        == 1
    )


def test_ensure_instance_profile_for_role_raises_when_role_does_not_exist() -> None:
    gateway = FakeIamGateway()
    use_case = EnsureInstanceProfileForRoleUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile=_PROFILE, region=_REGION
    )

    with pytest.raises(ResourceNotFoundError):
        use_case.execute("no-existe-este-rol")

    assert not gateway.instance_profiles

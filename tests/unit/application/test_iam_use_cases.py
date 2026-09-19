"""Tests for the IAM use cases' business rules, using hand-written fakes (no Mock())."""

import logging

import pytest
from aws_admin_cli.application.dto.iam import (
    AddUserToGroupRequest,
    AttachPolicyRequest,
    CopyUserRequest,
    CreateAccessKeyRequest,
    CreateGroupRequest,
    CreatePolicyRequest,
    CreateRoleRequest,
    CreateUserRequest,
    DeleteAccessKeyRequest,
    DeleteGroupRequest,
    DeleteLoginProfileRequest,
    DeletePolicyRequest,
    DeleteRoleRequest,
    DeleteUserRequest,
    DetachPolicyRequest,
    RemoveUserFromGroupRequest,
    SetLoginProfileRequest,
    UpdateAccessKeyRequest,
    UpdateUserRequest,
)
from aws_admin_cli.application.use_cases.iam.add_user_to_group import AddUserToGroupUseCase
from aws_admin_cli.application.use_cases.iam.attach_policy import AttachPolicyUseCase
from aws_admin_cli.application.use_cases.iam.copy_user import CopyUserUseCase
from aws_admin_cli.application.use_cases.iam.create_access_key import CreateAccessKeyUseCase
from aws_admin_cli.application.use_cases.iam.create_group import CreateGroupUseCase
from aws_admin_cli.application.use_cases.iam.create_policy import CreatePolicyUseCase
from aws_admin_cli.application.use_cases.iam.create_role import CreateRoleUseCase
from aws_admin_cli.application.use_cases.iam.create_user import (
    MANAGED_BY_TAG_KEY,
    MANAGED_BY_TAG_VALUE,
    CreateUserUseCase,
)
from aws_admin_cli.application.use_cases.iam.delete_access_key import DeleteAccessKeyUseCase
from aws_admin_cli.application.use_cases.iam.delete_group import DeleteGroupUseCase
from aws_admin_cli.application.use_cases.iam.delete_login_profile import (
    DeleteLoginProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.delete_policy import DeletePolicyUseCase
from aws_admin_cli.application.use_cases.iam.delete_role import DeleteRoleUseCase
from aws_admin_cli.application.use_cases.iam.delete_user import DeleteUserUseCase
from aws_admin_cli.application.use_cases.iam.detach_policy import DetachPolicyUseCase
from aws_admin_cli.application.use_cases.iam.get_policy import GetPolicyUseCase
from aws_admin_cli.application.use_cases.iam.get_policy_document import GetPolicyDocumentUseCase
from aws_admin_cli.application.use_cases.iam.get_role import GetRoleUseCase
from aws_admin_cli.application.use_cases.iam.get_user import GetUserUseCase
from aws_admin_cli.application.use_cases.iam.get_user_detail import GetUserDetailUseCase
from aws_admin_cli.application.use_cases.iam.list_access_keys import ListAccessKeysUseCase
from aws_admin_cli.application.use_cases.iam.list_attached_policies import (
    ListAttachedPoliciesUseCase,
)
from aws_admin_cli.application.use_cases.iam.list_groups_for_user import (
    ListGroupsForUserUseCase,
)
from aws_admin_cli.application.use_cases.iam.list_policies import ListPoliciesUseCase
from aws_admin_cli.application.use_cases.iam.list_roles import ListRolesUseCase
from aws_admin_cli.application.use_cases.iam.list_users import ListUsersUseCase
from aws_admin_cli.application.use_cases.iam.remove_user_from_group import (
    RemoveUserFromGroupUseCase,
)
from aws_admin_cli.application.use_cases.iam.set_login_profile import SetLoginProfileUseCase
from aws_admin_cli.application.use_cases.iam.update_access_key import UpdateAccessKeyUseCase
from aws_admin_cli.application.use_cases.iam.update_user import UpdateUserUseCase
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.iam import PolicyDocument, service_trust_policy

from tests.fakes.iam import FakeIamGateway, InMemoryRepository


def _wildcard_document() -> PolicyDocument:
    return PolicyDocument.model_validate(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}],
        }
    )


def _scoped_document() -> PolicyDocument:
    return PolicyDocument.model_validate(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::x/*"}
            ],
        }
    )


# -- create_user --------------------------------------------------------------


def test_create_user_if_not_exists_returns_existing_without_error() -> None:
    gateway = FakeIamGateway()
    repository = InMemoryRepository()
    use_case = CreateUserUseCase(
        gateway=gateway, repository=repository, profile="localstack", region="us-east-1"
    )
    first = use_case.execute(CreateUserRequest(name="alice"))

    second = use_case.execute(CreateUserRequest(name="alice", if_not_exists=True))

    assert second == first
    assert len(gateway.users) == 1


def test_create_user_adds_managed_by_tag_automatically() -> None:
    gateway = FakeIamGateway()
    use_case = CreateUserUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", region="us-east-1"
    )

    user = use_case.execute(CreateUserRequest(name="alice", tags={"Owner": "ruben"}))

    tag_dict = {tag["Key"]: tag["Value"] for tag in user.tags}
    assert tag_dict["Owner"] == "ruben"
    assert tag_dict[MANAGED_BY_TAG_KEY] == MANAGED_BY_TAG_VALUE


def test_create_user_writes_resource_record() -> None:
    repository = InMemoryRepository()
    use_case = CreateUserUseCase(
        gateway=FakeIamGateway(), repository=repository, profile="localstack", region="us-east-1"
    )

    user = use_case.execute(CreateUserRequest(name="alice"))

    record = repository.get(f"iam:user:{user.user_name}")
    assert record is not None
    assert record.arn == user.arn
    assert record.profile == "localstack"


def test_create_user_sanitizes_spaces_in_name() -> None:
    gateway = FakeIamGateway()
    use_case = CreateUserUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", region="us-east-1"
    )

    user = use_case.execute(CreateUserRequest(name="Harold Ortega"))

    assert user.user_name == "Harold_Ortega"
    assert "Harold_Ortega" in gateway.users
    assert "Harold Ortega" not in gateway.users


def test_create_user_sanitizes_path_without_slashes() -> None:
    gateway = FakeIamGateway()
    use_case = CreateUserUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", region="us-east-1"
    )

    user = use_case.execute(CreateUserRequest(name="alice", path="Admin"))

    assert user.path == "/Admin/"


def test_create_user_if_not_exists_finds_existing_by_sanitized_name() -> None:
    """A second create with the same (space-containing) name must still be
    idempotent -- the existing-check has to compare against the sanitized
    name, not the raw one the caller typed.
    """
    gateway = FakeIamGateway()
    use_case = CreateUserUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", region="us-east-1"
    )
    first = use_case.execute(CreateUserRequest(name="Harold Ortega"))

    second = use_case.execute(CreateUserRequest(name="Harold Ortega", if_not_exists=True))

    assert second == first
    assert len(gateway.users) == 1


def test_create_user_with_invalid_character_still_raises() -> None:
    """Sanitization only fixes spaces -- a semicolon (outside AWS's allowed
    character set) must still be rejected, not silently guessed at.
    """
    use_case = CreateUserUseCase(
        gateway=FakeIamGateway(),
        repository=InMemoryRepository(),
        profile="localstack",
        region="us-east-1",
    )

    with pytest.raises(ValidationError):
        use_case.execute(CreateUserRequest(name="alice;bob"))


# -- delete_user ----------------------------------------------------------------


def test_delete_user_with_attached_policies_and_no_force_raises_and_deletes_nothing() -> None:
    gateway = FakeIamGateway()
    create_user = CreateUserUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", region="us-east-1"
    )
    create_user.execute(CreateUserRequest(name="alice"))
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    gateway.attach_user_policy("alice", policy.arn)

    use_case = DeleteUserUseCase(gateway=gateway, repository=InMemoryRepository())

    with pytest.raises(ValidationError):
        use_case.execute(DeleteUserRequest(name="alice"))

    assert "alice" in gateway.users
    assert policy.arn in gateway.user_policies["alice"]


def test_delete_user_with_force_detaches_all_and_deletes() -> None:
    gateway = FakeIamGateway()
    repository = InMemoryRepository()
    create_user = CreateUserUseCase(
        gateway=gateway, repository=repository, profile="localstack", region="us-east-1"
    )
    create_user.execute(CreateUserRequest(name="alice"))
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    gateway.attach_user_policy("alice", policy.arn)

    use_case = DeleteUserUseCase(gateway=gateway, repository=repository)
    use_case.execute(DeleteUserRequest(name="alice", force=True))

    assert "alice" not in gateway.users
    assert repository.get("iam:user:alice") is None


# -- create_policy --------------------------------------------------------------


def test_create_policy_with_full_wildcard_and_no_flag_raises() -> None:
    use_case = CreatePolicyUseCase(
        gateway=FakeIamGateway(),
        repository=InMemoryRepository(),
        profile="localstack",
        region="us-east-1",
    )

    with pytest.raises(ValidationError):
        use_case.execute(CreatePolicyRequest(name="admin", document=_wildcard_document()))


def test_create_policy_with_full_wildcard_and_allow_flag_succeeds() -> None:
    gateway = FakeIamGateway()
    use_case = CreatePolicyUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", region="us-east-1"
    )

    policy = use_case.execute(
        CreatePolicyRequest(name="admin", document=_wildcard_document(), allow_wildcard=True)
    )

    assert policy.policy_name == "admin"
    assert policy.arn in gateway.policies


# -- delete_policy --------------------------------------------------------------


def test_delete_policy_with_attachments_raises() -> None:
    gateway = FakeIamGateway()
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    gateway.users["alice"] = gateway.create_user("alice", "/", [])
    gateway.attach_user_policy("alice", policy.arn)

    use_case = DeletePolicyUseCase(gateway=gateway, repository=InMemoryRepository())

    with pytest.raises(ValidationError):
        use_case.execute(DeletePolicyRequest(arn=policy.arn))

    assert policy.arn in gateway.policies


# -- attach_policy ----------------------------------------------------------------


def test_attach_policy_is_idempotent() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    logger = logging.getLogger("test")
    use_case = AttachPolicyUseCase(gateway=gateway, logger=logger)
    request = AttachPolicyRequest(
        principal_name="alice", policy_arn=policy.arn, principal_type="user"
    )

    use_case.execute(request)
    use_case.execute(request)

    assert gateway.user_policies["alice"] == {policy.arn}


def test_attach_policy_dispatches_to_user_or_role_by_principal_type() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    gateway.create_role("demo-role", _wildcard_document(), "/", None, None)
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    use_case = AttachPolicyUseCase(gateway=gateway, logger=logging.getLogger("test"))

    use_case.execute(
        AttachPolicyRequest(principal_name="alice", policy_arn=policy.arn, principal_type="user")
    )
    use_case.execute(
        AttachPolicyRequest(
            principal_name="demo-role", policy_arn=policy.arn, principal_type="role"
        )
    )

    assert policy.arn in gateway.user_policies["alice"]
    assert policy.arn in gateway.role_policies["demo-role"]
    assert policy.arn not in gateway.user_policies.get("demo-role", set())


def test_detach_policy_dispatches_to_user_or_role_by_principal_type() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    gateway.attach_user_policy("alice", policy.arn)
    use_case = DetachPolicyUseCase(gateway=gateway)

    use_case.execute(
        DetachPolicyRequest(principal_name="alice", policy_arn=policy.arn, principal_type="user")
    )

    assert policy.arn not in gateway.user_policies["alice"]


# -- create_role / delete_role --------------------------------------------------


def test_create_role_without_assume_role_statement_raises() -> None:
    use_case = CreateRoleUseCase(
        gateway=FakeIamGateway(),
        repository=InMemoryRepository(),
        profile="localstack",
        region="us-east-1",
    )

    with pytest.raises(ValidationError):
        use_case.execute(CreateRoleRequest(name="demo-role", trust_policy=_scoped_document()))


def test_create_role_if_not_exists_returns_existing_without_error() -> None:
    gateway = FakeIamGateway()
    use_case = CreateRoleUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", region="us-east-1"
    )
    trust = service_trust_policy("ec2.amazonaws.com")
    first = use_case.execute(CreateRoleRequest(name="demo-role", trust_policy=trust))

    second = use_case.execute(
        CreateRoleRequest(name="demo-role", trust_policy=trust, if_not_exists=True)
    )

    assert second == first
    assert len(gateway.roles) == 1


def test_create_role_writes_resource_record() -> None:
    repository = InMemoryRepository()
    use_case = CreateRoleUseCase(
        gateway=FakeIamGateway(), repository=repository, profile="localstack", region="us-east-1"
    )

    role = use_case.execute(
        CreateRoleRequest(name="demo-role", trust_policy=service_trust_policy("ec2.amazonaws.com"))
    )

    record = repository.get(f"iam:role:{role.role_name}")
    assert record is not None
    assert record.arn == role.arn


def test_delete_role_with_attached_policies_and_no_force_raises_and_deletes_nothing() -> None:
    gateway = FakeIamGateway()
    create_role = CreateRoleUseCase(
        gateway=gateway, repository=InMemoryRepository(), profile="localstack", region="us-east-1"
    )
    create_role.execute(
        CreateRoleRequest(name="demo-role", trust_policy=service_trust_policy("ec2.amazonaws.com"))
    )
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    gateway.attach_role_policy("demo-role", policy.arn)

    use_case = DeleteRoleUseCase(gateway=gateway, repository=InMemoryRepository())

    with pytest.raises(ValidationError):
        use_case.execute(DeleteRoleRequest(name="demo-role"))

    assert "demo-role" in gateway.roles


def test_delete_role_with_force_detaches_all_and_deletes() -> None:
    gateway = FakeIamGateway()
    repository = InMemoryRepository()
    create_role = CreateRoleUseCase(
        gateway=gateway, repository=repository, profile="localstack", region="us-east-1"
    )
    create_role.execute(
        CreateRoleRequest(name="demo-role", trust_policy=service_trust_policy("ec2.amazonaws.com"))
    )
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    gateway.attach_role_policy("demo-role", policy.arn)

    use_case = DeleteRoleUseCase(gateway=gateway, repository=repository)
    use_case.execute(DeleteRoleRequest(name="demo-role", force=True))

    assert "demo-role" not in gateway.roles
    assert repository.get("iam:role:demo-role") is None


# -- simple passthrough use cases -----------------------------------------------


def test_get_and_list_users() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])

    assert GetUserUseCase(gateway=gateway).execute("alice").user_name == "alice"
    assert [u.user_name for u in ListUsersUseCase(gateway=gateway).execute()] == ["alice"]


def test_get_and_list_policies_and_document() -> None:
    gateway = FakeIamGateway()
    document = _scoped_document()
    policy = gateway.create_policy("demo", document, "/", None)

    assert GetPolicyUseCase(gateway=gateway).execute(policy.arn).policy_name == "demo"
    assert [p.arn for p in ListPoliciesUseCase(gateway=gateway).execute()] == [policy.arn]
    assert GetPolicyDocumentUseCase(gateway=gateway).execute(policy.arn) == document


def test_get_and_list_roles() -> None:
    gateway = FakeIamGateway()
    gateway.create_role("demo-role", service_trust_policy("ec2.amazonaws.com"), "/", None, None)

    assert GetRoleUseCase(gateway=gateway).execute("demo-role").role_name == "demo-role"
    assert [r.role_name for r in ListRolesUseCase(gateway=gateway).execute()] == ["demo-role"]


def test_list_attached_policies_for_user_and_role() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    gateway.create_role("demo-role", service_trust_policy("ec2.amazonaws.com"), "/", None, None)
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    gateway.attach_user_policy("alice", policy.arn)
    gateway.attach_role_policy("demo-role", policy.arn)
    use_case = ListAttachedPoliciesUseCase(gateway=gateway)

    assert [p.policy_arn for p in use_case.execute("alice", "user")] == [policy.arn]
    assert [p.policy_arn for p in use_case.execute("demo-role", "role")] == [policy.arn]


# -- Groups -------------------------------------------------------------------------


def test_create_group_is_idempotent_with_if_not_exists() -> None:

    gateway = FakeIamGateway()
    use_case = CreateGroupUseCase(gateway=gateway)

    first = use_case.execute(CreateGroupRequest(name="engineers", if_not_exists=True))
    second = use_case.execute(CreateGroupRequest(name="engineers", if_not_exists=True))

    assert first.group_name == second.group_name == "engineers"
    assert len(gateway.groups) == 1


def test_add_and_remove_user_from_group_and_list_groups_for_user() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    gateway.create_group("engineers", "/")

    AddUserToGroupUseCase(gateway=gateway).execute(
        AddUserToGroupRequest(group_name="engineers", user_name="alice")
    )
    assert [g.group_name for g in ListGroupsForUserUseCase(gateway=gateway).execute("alice")] == [
        "engineers"
    ]

    RemoveUserFromGroupUseCase(gateway=gateway).execute(
        RemoveUserFromGroupRequest(group_name="engineers", user_name="alice")
    )
    assert ListGroupsForUserUseCase(gateway=gateway).execute("alice") == []


def test_delete_group_with_members_requires_force() -> None:

    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    gateway.create_group("engineers", "/")
    gateway.add_user_to_group("engineers", "alice")
    use_case = DeleteGroupUseCase(gateway=gateway)

    with pytest.raises(ValidationError):
        use_case.execute(DeleteGroupRequest(name="engineers", force=False))
    assert "engineers" in gateway.groups

    use_case.execute(DeleteGroupRequest(name="engineers", force=True))
    assert "engineers" not in gateway.groups
    assert gateway.list_groups_for_user("alice") == []


# -- Login profile and access keys ---------------------------------------------------


def test_set_login_profile_creates_then_updates() -> None:

    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    use_case = SetLoginProfileUseCase(gateway=gateway)

    created = use_case.execute(
        SetLoginProfileRequest(user_name="alice", password="P4ss!", password_reset_required=True)
    )
    assert created.password_reset_required is True

    updated = use_case.execute(
        SetLoginProfileRequest(
            user_name="alice", password="New-P4ss!", password_reset_required=False
        )
    )
    assert updated.password_reset_required is False
    assert len(gateway.login_profiles) == 1  # still one profile, not a duplicate


def test_delete_login_profile() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    gateway.create_login_profile("alice", "P4ss!", password_reset_required=True)

    DeleteLoginProfileUseCase(gateway=gateway).execute(DeleteLoginProfileRequest("alice"))

    assert gateway.get_login_profile("alice") is None


def test_access_key_create_list_update_delete() -> None:

    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])

    created = CreateAccessKeyUseCase(gateway=gateway).execute(
        CreateAccessKeyRequest(user_name="alice")
    )
    assert created.secret_access_key

    listed = ListAccessKeysUseCase(gateway=gateway).execute("alice")
    assert [k.access_key_id for k in listed] == [created.access_key_id]

    UpdateAccessKeyUseCase(gateway=gateway).execute(
        UpdateAccessKeyRequest(user_name="alice", access_key_id=created.access_key_id, active=False)
    )
    assert ListAccessKeysUseCase(gateway=gateway).execute("alice")[0].status == "Inactive"

    DeleteAccessKeyUseCase(gateway=gateway).execute(
        DeleteAccessKeyRequest(user_name="alice", access_key_id=created.access_key_id)
    )
    assert ListAccessKeysUseCase(gateway=gateway).execute("alice") == []


# -- Copy user and user detail --------------------------------------------------------


def test_copy_user_clones_tags_and_groups() -> None:

    gateway = FakeIamGateway()
    repository = InMemoryRepository()
    source = gateway.create_user("alice", "/", [{"Key": "Team", "Value": "platform"}])
    gateway.create_group("engineers", "/")
    gateway.add_user_to_group("engineers", "alice")
    create_user = CreateUserUseCase(
        gateway=gateway, repository=repository, profile="test", region="us-east-1"
    )
    use_case = CopyUserUseCase(gateway=gateway, create_user=create_user)

    copy = use_case.execute(CopyUserRequest(source_name="alice", new_name="bob"))

    assert copy.user_name == "bob"
    copy_tags = {t["Key"]: t["Value"] for t in copy.tags}
    assert copy_tags["Team"] == "platform"
    assert [g.group_name for g in gateway.list_groups_for_user("bob")] == ["engineers"]
    assert source.user_name == "alice"  # the original is untouched


def test_get_user_detail_aggregates_everything() -> None:

    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    gateway.create_group("engineers", "/")
    gateway.add_user_to_group("engineers", "alice")
    gateway.create_login_profile("alice", "P4ss!", password_reset_required=True)
    gateway.create_access_key("alice")
    policy = gateway.create_policy("demo", _scoped_document(), "/", None)
    gateway.attach_user_policy("alice", policy.arn)

    detail = GetUserDetailUseCase(gateway=gateway).execute("alice")

    assert detail.user.user_name == "alice"
    assert [g.group_name for g in detail.groups] == ["engineers"]
    assert detail.login_profile is not None
    assert len(detail.access_keys) == 1
    assert [p.policy_arn for p in detail.attached_policies] == [policy.arn]


# -- update_user ------------------------------------------------------------------


def test_update_user_renames_and_changes_path() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    use_case = UpdateUserUseCase(gateway=gateway)

    updated = use_case.execute(
        UpdateUserRequest(name="alice", new_name="alice2", new_path="/renamed/")
    )

    assert updated.user_name == "alice2"
    assert updated.path == "/renamed/"
    assert "alice" not in gateway.users
    assert "alice2" in gateway.users


def test_update_user_sanitizes_spaces_in_new_name() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    use_case = UpdateUserUseCase(gateway=gateway)

    updated = use_case.execute(UpdateUserRequest(name="alice", new_name="Harold Ortega"))

    assert updated.user_name == "Harold_Ortega"


def test_update_user_sanitizes_new_path_without_slashes() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    use_case = UpdateUserUseCase(gateway=gateway)

    updated = use_case.execute(UpdateUserRequest(name="alice", new_path="Admin"))

    assert updated.path == "/Admin/"


def test_update_user_with_invalid_new_name_character_still_raises() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    use_case = UpdateUserUseCase(gateway=gateway)

    with pytest.raises(ValidationError):
        use_case.execute(UpdateUserRequest(name="alice", new_name="bob;carol"))


# -- delete_user: extended blockers (groups, console access, access keys) -----------


def test_delete_user_blocked_by_group_membership_without_force() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    gateway.create_group("engineers", "/")
    gateway.add_user_to_group("engineers", "alice")
    use_case = DeleteUserUseCase(gateway=gateway, repository=InMemoryRepository())

    with pytest.raises(ValidationError):
        use_case.execute(DeleteUserRequest(name="alice"))
    assert "alice" in gateway.users

    use_case.execute(DeleteUserRequest(name="alice", force=True))
    assert "alice" not in gateway.users


def test_delete_user_blocked_by_console_access_without_force() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    gateway.create_login_profile("alice", "P4ss!", password_reset_required=True)
    use_case = DeleteUserUseCase(gateway=gateway, repository=InMemoryRepository())

    with pytest.raises(ValidationError):
        use_case.execute(DeleteUserRequest(name="alice"))

    use_case.execute(DeleteUserRequest(name="alice", force=True))
    assert "alice" not in gateway.users
    assert gateway.get_login_profile("alice") is None


def test_delete_user_blocked_by_access_keys_without_force() -> None:
    gateway = FakeIamGateway()
    gateway.create_user("alice", "/", [])
    gateway.create_access_key("alice")
    use_case = DeleteUserUseCase(gateway=gateway, repository=InMemoryRepository())

    with pytest.raises(ValidationError):
        use_case.execute(DeleteUserRequest(name="alice"))

    use_case.execute(DeleteUserRequest(name="alice", force=True))
    assert "alice" not in gateway.users
    assert gateway.list_access_keys("alice") == []

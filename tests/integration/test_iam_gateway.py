"""Integration tests for Boto3IamGateway, against moto (no real AWS or LocalStack)."""

import pytest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.exceptions import ResourceAlreadyExistsError, ResourceNotFoundError
from aws_admin_cli.domain.models.iam import PolicyDocument, service_trust_policy
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.gateways.boto3_iam_gateway import Boto3IamGateway
from aws_admin_cli.infrastructure.aws.session_factory import Boto3SessionFactory
from moto import mock_aws


@pytest.fixture
def gateway() -> Boto3IamGateway:
    settings = Settings(profile="testprofile", region="us-east-1")
    client_factory = ClientFactory(
        session_factory=Boto3SessionFactory(profile=settings.profile, region=settings.region),
        settings=settings,
    )
    return Boto3IamGateway(client_factory=client_factory)


def _scoped_document() -> PolicyDocument:
    return PolicyDocument.model_validate(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::x/*"}
            ],
        }
    )


@mock_aws
def test_user_full_lifecycle(gateway: Boto3IamGateway) -> None:
    created = gateway.create_user("alice", "/", [{"Key": "Team", "Value": "platform"}])
    assert created.user_name == "alice"

    fetched = gateway.get_user("alice")
    assert fetched.arn == created.arn

    listed = gateway.list_users(None)
    assert any(user.user_name == "alice" for user in listed)

    gateway.delete_user("alice")

    with pytest.raises(ResourceNotFoundError):
        gateway.get_user("alice")


@mock_aws
def test_update_user_renames_and_changes_path(gateway: Boto3IamGateway) -> None:
    gateway.create_user("alice", "/", [])

    updated = gateway.update_user("alice", "alice2", "/renamed/")

    assert updated.user_name == "alice2"
    assert updated.path == "/renamed/"
    with pytest.raises(ResourceNotFoundError):
        gateway.get_user("alice")
    assert gateway.get_user("alice2").user_name == "alice2"


@mock_aws
def test_create_duplicate_user_raises_resource_already_exists(gateway: Boto3IamGateway) -> None:
    gateway.create_user("alice", "/", [])

    with pytest.raises(ResourceAlreadyExistsError) as exc_info:
        gateway.create_user("alice", "/", [])

    assert exc_info.value.aws_code == "EntityAlreadyExists"


@mock_aws
def test_get_missing_user_raises_resource_not_found(gateway: Boto3IamGateway) -> None:
    with pytest.raises(ResourceNotFoundError):
        gateway.get_user("nobody")


@mock_aws
def test_policy_full_lifecycle_with_document_roundtrip(gateway: Boto3IamGateway) -> None:
    document = _scoped_document()
    created = gateway.create_policy("demo-policy", document, "/", "a policy")
    assert created.policy_name == "demo-policy"

    fetched = gateway.get_policy(created.arn)
    assert fetched.arn == created.arn

    fetched_document = gateway.get_policy_document(created.arn, None)
    assert fetched_document == document

    listed = gateway.list_policies("Local", False)
    assert any(policy.arn == created.arn for policy in listed)

    gateway.delete_policy(created.arn)
    with pytest.raises(ResourceNotFoundError):
        gateway.get_policy(created.arn)


@mock_aws
def test_role_full_lifecycle_decodes_trust_policy(gateway: Boto3IamGateway) -> None:
    trust_policy = service_trust_policy("ec2.amazonaws.com")
    created = gateway.create_role("demo-role", trust_policy, "/", "a role", 3600)
    assert created.assume_role_policy_document is not None
    assert created.assume_role_policy_document == trust_policy

    fetched = gateway.get_role("demo-role")
    assert fetched.assume_role_policy_document == trust_policy

    listed = gateway.list_roles(None)
    assert any(role.role_name == "demo-role" for role in listed)
    listed_role = next(role for role in listed if role.role_name == "demo-role")
    assert listed_role.assume_role_policy_document == trust_policy

    gateway.delete_role("demo-role")
    with pytest.raises(ResourceNotFoundError):
        gateway.get_role("demo-role")


@mock_aws
def test_attach_detach_policy_to_user_and_role(gateway: Boto3IamGateway) -> None:
    gateway.create_user("alice", "/", [])
    gateway.create_role("demo-role", service_trust_policy("ec2.amazonaws.com"), "/", None, None)
    policy = gateway.create_policy("demo-policy", _scoped_document(), "/", None)

    gateway.attach_user_policy("alice", policy.arn)
    assert [p.policy_arn for p in gateway.list_attached_user_policies("alice")] == [policy.arn]

    gateway.attach_role_policy("demo-role", policy.arn)
    assert [p.policy_arn for p in gateway.list_attached_role_policies("demo-role")] == [policy.arn]

    gateway.detach_user_policy("alice", policy.arn)
    assert gateway.list_attached_user_policies("alice") == []

    gateway.detach_role_policy("demo-role", policy.arn)
    assert gateway.list_attached_role_policies("demo-role") == []


@mock_aws
def test_list_users_returns_every_created_user(gateway: Boto3IamGateway) -> None:
    # NOTE: moto's IAM `list_users` mock does not truncate/paginate at all --
    # even a single, non-paginated call already returns every user regardless
    # of count, so this alone can't "catch" a gateway that forgot to paginate.
    # See tests/unit/infrastructure/test_boto3_iam_gateway_pagination.py for a
    # mock-based test that verifies `get_paginator(...)` is actually called.
    for i in range(15):
        gateway.create_user(f"user-{i:02d}", "/test/", [])

    users = gateway.list_users("/test/")

    assert len(users) == 15


# -- Instance profiles ------------------------------------------------------------


@mock_aws
def test_instance_profile_full_lifecycle(gateway: Boto3IamGateway) -> None:
    gateway.create_role("demo-ec2-role", service_trust_policy("ec2.amazonaws.com"), "/", None, None)

    created = gateway.create_instance_profile("demo-profile", "/")
    assert created.instance_profile_name == "demo-profile"
    assert created.roles == []

    gateway.add_role_to_instance_profile("demo-profile", "demo-ec2-role")
    fetched = gateway.get_instance_profile("demo-profile")
    assert fetched.role_name == "demo-ec2-role"
    assert fetched.roles[0].arn.endswith("role/demo-ec2-role")

    gateway.remove_role_from_instance_profile("demo-profile", "demo-ec2-role")
    assert gateway.get_instance_profile("demo-profile").role_name is None

    gateway.delete_instance_profile("demo-profile")
    with pytest.raises(ResourceNotFoundError):
        gateway.get_instance_profile("demo-profile")


@mock_aws
def test_create_duplicate_instance_profile_raises_resource_already_exists(
    gateway: Boto3IamGateway,
) -> None:
    gateway.create_instance_profile("demo-profile", "/")

    with pytest.raises(ResourceAlreadyExistsError):
        gateway.create_instance_profile("demo-profile", "/")


@mock_aws
def test_list_instance_profiles_is_fully_paginated(gateway: Boto3IamGateway) -> None:
    for i in range(15):
        gateway.create_instance_profile(f"profile-{i:02d}", "/test/")

    profiles = gateway.list_instance_profiles("/test/")

    assert len(profiles) == 15


# -- Groups ------------------------------------------------------------------------


@mock_aws
def test_group_full_lifecycle(gateway: Boto3IamGateway) -> None:
    gateway.create_user("alice", "/", [])
    created = gateway.create_group("engineers", "/")
    assert created.group_name == "engineers"

    gateway.add_user_to_group("engineers", "alice")
    assert [g.group_name for g in gateway.list_groups_for_user("alice")] == ["engineers"]
    assert [u.user_name for u in gateway.get_group_members("engineers")] == ["alice"]

    gateway.remove_user_from_group("engineers", "alice")
    assert gateway.list_groups_for_user("alice") == []
    assert gateway.get_group_members("engineers") == []

    gateway.delete_group("engineers")
    assert gateway.list_groups(None) == []


@mock_aws
def test_create_duplicate_group_raises_resource_already_exists(gateway: Boto3IamGateway) -> None:
    gateway.create_group("engineers", "/")

    with pytest.raises(ResourceAlreadyExistsError):
        gateway.create_group("engineers", "/")


@mock_aws
def test_list_groups_is_fully_paginated(gateway: Boto3IamGateway) -> None:
    for i in range(15):
        gateway.create_group(f"group-{i:02d}", "/test/")

    groups = gateway.list_groups("/test/")

    assert len(groups) == 15


# -- Login profile (console access) -------------------------------------------------


@mock_aws
def test_login_profile_full_lifecycle(gateway: Boto3IamGateway) -> None:
    gateway.create_user("alice", "/", [])
    assert gateway.get_login_profile("alice") is None

    created = gateway.create_login_profile("alice", "S3cur3-Pass!", password_reset_required=True)
    assert created.user_name == "alice"
    # NOT asserting created.password_reset_required here: moto's CreateLoginProfile
    # always echoes False regardless of the parameter it was given (a moto fidelity
    # gap, verified against real botocore semantics) -- UpdateLoginProfile (below)
    # does honor it correctly, which is what every other assertion here exercises.

    fetched = gateway.get_login_profile("alice")
    assert fetched is not None

    updated = gateway.update_login_profile(
        "alice", "New-Pass!23", password_reset_required=True
    )
    assert updated.password_reset_required is True

    updated_again = gateway.update_login_profile(
        "alice", "Another-Pass!45", password_reset_required=False
    )
    assert updated_again.password_reset_required is False

    gateway.delete_login_profile("alice")
    assert gateway.get_login_profile("alice") is None


@mock_aws
def test_create_duplicate_login_profile_raises_resource_already_exists(
    gateway: Boto3IamGateway,
) -> None:
    gateway.create_user("alice", "/", [])
    gateway.create_login_profile("alice", "S3cur3-Pass!", password_reset_required=True)

    with pytest.raises(ResourceAlreadyExistsError):
        gateway.create_login_profile("alice", "Other-Pass!23", password_reset_required=True)


# -- Access keys ---------------------------------------------------------------------


@mock_aws
def test_access_key_full_lifecycle(gateway: Boto3IamGateway) -> None:
    gateway.create_user("alice", "/", [])
    assert gateway.list_access_keys("alice") == []

    created = gateway.create_access_key("alice")
    assert created.status == "Active"
    assert created.secret_access_key  # only ever returned here

    listed = gateway.list_access_keys("alice")
    assert len(listed) == 1
    assert listed[0].access_key_id == created.access_key_id

    gateway.update_access_key("alice", created.access_key_id, active=False)
    assert gateway.list_access_keys("alice")[0].status == "Inactive"

    gateway.delete_access_key("alice", created.access_key_id)
    assert gateway.list_access_keys("alice") == []

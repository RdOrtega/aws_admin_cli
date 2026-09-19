"""Application-layer DTOs for IAM use cases.

Frozen dataclasses (consistent with ``ClientFactory``/``AppContext`` elsewhere
in the project) rather than Pydantic models: these carry validated,
already-parsed CLI input into a use case: there's no need for Pydantic's
coercion/serialization machinery here, just an immutable data holder.
"""

from dataclasses import dataclass, field
from typing import Literal

from aws_admin_cli.domain.models.iam import PolicyDocument


@dataclass(frozen=True, slots=True)
class CreateUserRequest:
    """Request to create an IAM user."""

    name: str
    path: str = "/"
    tags: dict[str, str] = field(default_factory=dict)
    if_not_exists: bool = False


@dataclass(frozen=True, slots=True)
class DeleteUserRequest:
    """Request to delete an IAM user."""

    name: str
    force: bool = False


@dataclass(frozen=True, slots=True)
class UpdateUserRequest:
    """Request to rename a user and/or change its path."""

    name: str
    new_name: str | None = None
    new_path: str | None = None


@dataclass(frozen=True, slots=True)
class SetUserTagsRequest:
    """Request to add or overwrite tags on an existing user."""

    name: str
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeleteUserTagsRequest:
    """Request to remove tags (by key) from a user."""

    name: str
    keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CopyUserRequest:
    """Request to create a new user by cloning an existing one's groups and tags."""

    source_name: str
    new_name: str
    path: str = "/"


@dataclass(frozen=True, slots=True)
class CreateGroupRequest:
    """Request to create an IAM group."""

    name: str
    path: str = "/"
    if_not_exists: bool = False


@dataclass(frozen=True, slots=True)
class DeleteGroupRequest:
    """Request to delete an IAM group."""

    name: str
    force: bool = False


@dataclass(frozen=True, slots=True)
class AddUserToGroupRequest:
    """Request to add a user to a group."""

    group_name: str
    user_name: str


@dataclass(frozen=True, slots=True)
class RemoveUserFromGroupRequest:
    """Request to remove a user from a group."""

    group_name: str
    user_name: str


@dataclass(frozen=True, slots=True)
class SetLoginProfileRequest:
    """Request to grant/update a user's console (password) access.

    A single "upsert": creates the login profile if the user has none yet,
    or updates the existing one otherwise -- the caller never needs to know
    which case applies.
    """

    user_name: str
    password: str
    password_reset_required: bool = True


@dataclass(frozen=True, slots=True)
class DeleteLoginProfileRequest:
    """Request to revoke a user's console access entirely."""

    user_name: str


@dataclass(frozen=True, slots=True)
class CreateAccessKeyRequest:
    """Request to create a new access key for a user."""

    user_name: str


@dataclass(frozen=True, slots=True)
class UpdateAccessKeyRequest:
    """Request to activate or deactivate an access key."""

    user_name: str
    access_key_id: str
    active: bool


@dataclass(frozen=True, slots=True)
class DeleteAccessKeyRequest:
    """Request to permanently delete an access key."""

    user_name: str
    access_key_id: str


@dataclass(frozen=True, slots=True)
class DeactivateMfaDeviceRequest:
    """Request to unlink a user's MFA device -- and, if virtual, delete it outright.

    ``delete_virtual_device=True`` is "Reset": deactivate, then also delete
    the device object if it's virtual (a hardware device has no delete API
    and is only ever deactivated). ``False`` is a plain "Deactivate": unlink
    only, the object -- virtual or hardware -- is left alone.
    """

    user_name: str
    serial_number: str
    delete_virtual_device: bool = False


@dataclass(frozen=True, slots=True)
class CreatePolicyRequest:
    """Request to create a customer-managed IAM policy."""

    name: str
    document: PolicyDocument
    path: str = "/"
    description: str | None = None
    allow_wildcard: bool = False


@dataclass(frozen=True, slots=True)
class DeletePolicyRequest:
    """Request to delete a customer-managed IAM policy."""

    arn: str
    force: bool = False


@dataclass(frozen=True, slots=True)
class CreateRoleRequest:
    """Request to create an IAM role."""

    name: str
    trust_policy: PolicyDocument
    path: str = "/"
    description: str | None = None
    max_session_duration: int | None = None
    if_not_exists: bool = False


@dataclass(frozen=True, slots=True)
class DeleteRoleRequest:
    """Request to delete an IAM role."""

    name: str
    force: bool = False


@dataclass(frozen=True, slots=True)
class AttachPolicyRequest:
    """Request to attach a managed policy to a user or role."""

    principal_name: str
    policy_arn: str
    principal_type: Literal["user", "role"]


@dataclass(frozen=True, slots=True)
class DetachPolicyRequest:
    """Request to detach a managed policy from a user or role."""

    principal_name: str
    policy_arn: str
    principal_type: Literal["user", "role"]


@dataclass(frozen=True, slots=True)
class CreateInstanceProfileRequest:
    """Request to create an IAM instance profile."""

    name: str
    path: str = "/"
    if_not_exists: bool = False


@dataclass(frozen=True, slots=True)
class DeleteInstanceProfileRequest:
    """Request to delete an IAM instance profile."""

    name: str
    force: bool = False


@dataclass(frozen=True, slots=True)
class AttachRoleToProfileRequest:
    """Request to attach a role to an instance profile."""

    profile_name: str
    role_name: str

"""The IamGateway port: IAM operations, returning domain models only.

Implementations must never leak a raw AWS SDK value across this boundary --
every method returns an ``aws_admin_cli.domain.models.iam`` model (or
``None``/``list``/nothing), and raises only ``aws_admin_cli.core.exceptions``
types (via ``infrastructure.aws.error_mapper.aws_error_boundary``), never a
raw client-error exception from the underlying SDK.
"""

from datetime import datetime
from typing import Literal, Protocol, Self

from aws_admin_cli.domain.models.iam import (
    AccessKey,
    AccessKeyMetadata,
    AttachedPolicy,
    IamGroup,
    IamInstanceProfile,
    IamPolicy,
    IamRole,
    IamUser,
    LoginProfile,
    PolicyDocument,
)


class IamGateway(Protocol):
    """Port: IAM operations for users, policies, and roles."""

    # -- Users ------------------------------------------------------------

    def create_user(self: Self, name: str, path: str, tags: list[dict[str, str]]) -> IamUser:
        """Create an IAM user."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_user(self: Self, name: str) -> IamUser:
        """Fetch a single IAM user by name."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_users(self: Self, path_prefix: str | None) -> list[IamUser]:
        """List every IAM user (fully paginated), optionally filtered by path prefix."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_user(self: Self, name: str) -> None:
        """Delete an IAM user. The caller is responsible for detaching policies first."""
        ...  # pragma: no cover -- Protocol body, never executed

    def update_user(
        self: Self, name: str, new_name: str | None, new_path: str | None
    ) -> IamUser:
        """Rename a user and/or change its path. ``None`` for either means "leave as-is"."""
        ...  # pragma: no cover -- Protocol body, never executed

    def tag_user(self: Self, name: str, tags: dict[str, str]) -> None:
        """Add tags to a user, overwriting any existing tag that shares a key."""
        ...  # pragma: no cover -- Protocol body, never executed

    def untag_user(self: Self, name: str, keys: list[str]) -> None:
        """Remove tags by key from a user (values, if any, are ignored)."""
        ...  # pragma: no cover -- Protocol body, never executed

    # -- Groups ---------------------------------------------------------------

    def create_group(self: Self, name: str, path: str) -> IamGroup:
        """Create an IAM group."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_groups(self: Self, path_prefix: str | None) -> list[IamGroup]:
        """List every IAM group (fully paginated), optionally filtered by path prefix."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_group(self: Self, name: str) -> None:
        """Delete an IAM group. The caller is responsible for removing its members first."""
        ...  # pragma: no cover -- Protocol body, never executed

    def add_user_to_group(self: Self, group_name: str, user_name: str) -> None:
        """Add a user to a group."""
        ...  # pragma: no cover -- Protocol body, never executed

    def remove_user_from_group(self: Self, group_name: str, user_name: str) -> None:
        """Remove a user from a group."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_groups_for_user(self: Self, user_name: str) -> list[IamGroup]:
        """List every group a user belongs to (fully paginated)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_group_members(self: Self, name: str) -> list[IamUser]:
        """List every user that belongs to a group (fully paginated)."""
        ...  # pragma: no cover -- Protocol body, never executed

    # -- Console (login profile) and programmatic (access key) access -------------

    def create_login_profile(
        self: Self, user_name: str, password: str, *, password_reset_required: bool
    ) -> LoginProfile:
        """Grant a user console access by setting an initial password.

        Raises:
            ResourceAlreadyExistsError: The user already has a login profile
                (use ``update_login_profile`` to change its password/flag).
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def update_login_profile(
        self: Self,
        user_name: str,
        password: str | None,
        *,
        password_reset_required: bool | None,
    ) -> LoginProfile:
        """Update a user's existing console password and/or reset-required flag.

        ``None`` for either argument means "leave that field unchanged" --
        AWS itself requires at least one of the two to be set.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def get_login_profile(self: Self, user_name: str) -> LoginProfile | None:
        """Fetch a user's login profile, or ``None`` if they have no console access."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_login_profile(self: Self, user_name: str) -> None:
        """Revoke a user's console access entirely."""
        ...  # pragma: no cover -- Protocol body, never executed

    def create_access_key(self: Self, user_name: str) -> AccessKey:
        """Create a new access key for a user. The secret is returned ONLY here."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_access_keys(self: Self, user_name: str) -> list[AccessKeyMetadata]:
        """List a user's access keys (metadata only -- never the secret)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def update_access_key(self: Self, user_name: str, access_key_id: str, *, active: bool) -> None:
        """Activate or deactivate an access key, without deleting it."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_access_key(self: Self, user_name: str, access_key_id: str) -> None:
        """Permanently delete an access key."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_access_key_last_used(self: Self, access_key_id: str) -> datetime | None:
        """Fetch when ``access_key_id`` was last used, or ``None`` if never used.

        Backs the "Last API Activity" audit signal -- ``ListAccessKeys``
        itself carries no usage timestamp; AWS only exposes it through this
        separate ``GetAccessKeyLastUsed`` call, keyed by the key ID alone
        (no username needed).
        """
        ...  # pragma: no cover -- Protocol body, never executed

    # -- MFA ------------------------------------------------------------------

    def list_mfa_devices(self: Self, user_name: str) -> list[str]:
        """List the serial numbers of every MFA device enabled for a user.

        Backs the user detail screen's "MFA Enabled" column -- an empty list
        means no MFA device is registered, never an error.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def deactivate_mfa_device(self: Self, user_name: str, serial_number: str) -> None:
        """Unlink an MFA device from a user; the device object itself is untouched.

        The only removal call that applies to EVERY MFA device, hardware or
        virtual -- see ``delete_virtual_mfa_device`` for the one that isn't.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_virtual_mfa_device(self: Self, serial_number: str) -> None:
        """Permanently delete a virtual MFA device object.

        Only virtual devices (serial numbers shaped like
        ``arn:...:mfa/...``) can be deleted this way -- a hardware device has
        no delete API and can only ever be deactivated. Deleting one still
        requires deactivating it first if it's currently assigned to a user.
        """
        ...  # pragma: no cover -- Protocol body, never executed

    # -- Policies -----------------------------------------------------------

    def create_policy(
        self: Self,
        name: str,
        document: PolicyDocument,
        path: str,
        description: str | None,
    ) -> IamPolicy:
        """Create a customer-managed IAM policy."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_policy(self: Self, arn: str) -> IamPolicy:
        """Fetch a single IAM policy by ARN."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_policies(
        self: Self, scope: Literal["All", "AWS", "Local"], only_attached: bool
    ) -> list[IamPolicy]:
        """List IAM policies (fully paginated), filtered by scope/attachment."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_policy(self: Self, arn: str) -> None:
        """Delete a customer-managed IAM policy. Must have zero attachments."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_policy_document(self: Self, arn: str, version_id: str | None) -> PolicyDocument:
        """Fetch a policy's document, defaulting to its current default version."""
        ...  # pragma: no cover -- Protocol body, never executed

    # -- Roles ----------------------------------------------------------------

    def create_role(
        self: Self,
        name: str,
        trust_policy: PolicyDocument,
        path: str,
        description: str | None,
        max_session_duration: int | None,
    ) -> IamRole:
        """Create an IAM role."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_role(self: Self, name: str) -> IamRole:
        """Fetch a single IAM role by name."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_roles(self: Self, path_prefix: str | None) -> list[IamRole]:
        """List every IAM role (fully paginated), optionally filtered by path prefix."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_role(self: Self, name: str) -> None:
        """Delete an IAM role. The caller is responsible for detaching policies first."""
        ...  # pragma: no cover -- Protocol body, never executed

    # -- Attachments --------------------------------------------------------

    def attach_user_policy(self: Self, user_name: str, policy_arn: str) -> None:
        """Attach a managed policy to a user."""
        ...  # pragma: no cover -- Protocol body, never executed

    def detach_user_policy(self: Self, user_name: str, policy_arn: str) -> None:
        """Detach a managed policy from a user."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_attached_user_policies(self: Self, user_name: str) -> list[AttachedPolicy]:
        """List every policy attached to a user (fully paginated)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def attach_role_policy(self: Self, role_name: str, policy_arn: str) -> None:
        """Attach a managed policy to a role."""
        ...  # pragma: no cover -- Protocol body, never executed

    def detach_role_policy(self: Self, role_name: str, policy_arn: str) -> None:
        """Detach a managed policy from a role."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_attached_role_policies(self: Self, role_name: str) -> list[AttachedPolicy]:
        """List every policy attached to a role (fully paginated)."""
        ...  # pragma: no cover -- Protocol body, never executed

    # -- Instance profiles ------------------------------------------------------

    def create_instance_profile(self: Self, name: str, path: str) -> IamInstanceProfile:
        """Create an instance profile (with no role attached yet)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def get_instance_profile(self: Self, name: str) -> IamInstanceProfile:
        """Fetch a single instance profile by name, including its attached role(s)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def list_instance_profiles(self: Self, path_prefix: str | None) -> list[IamInstanceProfile]:
        """List every instance profile (fully paginated), optionally filtered by path prefix."""
        ...  # pragma: no cover -- Protocol body, never executed

    def delete_instance_profile(self: Self, name: str) -> None:
        """Delete an instance profile. The caller is responsible for removing roles first."""
        ...  # pragma: no cover -- Protocol body, never executed

    def add_role_to_instance_profile(self: Self, profile_name: str, role_name: str) -> None:
        """Attach a role to an instance profile (AWS allows at most one role per profile)."""
        ...  # pragma: no cover -- Protocol body, never executed

    def remove_role_from_instance_profile(self: Self, profile_name: str, role_name: str) -> None:
        """Detach a role from an instance profile."""
        ...  # pragma: no cover -- Protocol body, never executed

    # -- Credential Report --------------------------------------------------------

    def get_credential_report(self: Self) -> bytes:
        """Fetch the account's IAM credential report as raw CSV bytes.

        Generates a fresh report and waits for it to become ready (AWS's own
        asynchronous generate-then-fetch contract) before returning its
        content. Callers get back raw bytes -- parsing rows out of the CSV is
        ``GetCredentialReportUseCase``'s job, not this port's.
        """
        ...  # pragma: no cover -- Protocol body, never executed

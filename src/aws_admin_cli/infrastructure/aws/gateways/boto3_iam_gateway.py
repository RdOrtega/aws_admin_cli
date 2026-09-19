"""The boto3-backed ``IamGateway`` implementation.

Every method is wrapped in :func:`aws_error_boundary`, so nothing from
botocore ever crosses back into ``application/``. Listing operations are
fully paginated (``client.get_paginator(...)``) -- a ``list_users`` that only
returns the first page would be a correctness bug, not a simplification.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self, cast

from botocore.exceptions import ClientError

from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
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
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.error_mapper import aws_error_boundary

if TYPE_CHECKING:
    from aws_admin_cli.domain.ports.iam_gateway import IamGateway

_logger = logging.getLogger("aws_admin_cli")

_CREDENTIAL_REPORT_MAX_ATTEMPTS = 10
_CREDENTIAL_REPORT_POLL_SECONDS = 1.0


def _hydrate_role(raw: dict[str, Any]) -> IamRole:
    """Build an ``IamRole``, decoding its (possibly URL-encoded) trust policy first.

    Every role response -- ``create_role``, ``get_role``, and each entry from
    ``list_roles`` -- carries ``AssumeRolePolicyDocument`` as either a plain
    dict (moto) or a URL-encoded JSON string (real AWS); ``PolicyDocument.
    from_aws`` normalizes either into the nested model ``IamRole`` expects.
    """
    role_data = dict(raw)
    role_data["AssumeRolePolicyDocument"] = PolicyDocument.from_aws(
        role_data["AssumeRolePolicyDocument"]
    ).model_dump(by_alias=True)
    return IamRole.model_validate(role_data)


def _hydrate_instance_profile(raw: dict[str, Any]) -> IamInstanceProfile:
    """Build an ``IamInstanceProfile``, decoding each embedded role's trust policy first.

    Every ``InstanceProfile`` response embeds full ``Role`` objects in
    ``Roles`` -- each one carries the same URL-encoded-trust-policy quirk
    ``_hydrate_role`` handles for a standalone role, so it's reused here per
    embedded role rather than duplicated.
    """
    data = dict(raw)
    data["Roles"] = [_hydrate_role(role) for role in raw.get("Roles", [])]
    return IamInstanceProfile.model_validate(data)


@dataclass(frozen=True, slots=True)
class Boto3IamGateway:
    """``IamGateway`` implemented against a real (or LocalStack) IAM client."""

    client_factory: ClientFactory

    def _client(self: Self) -> Any:
        # `ClientFactory.iam()` already caches the underlying boto3 client, so
        # calling this per-method is cheap -- it's still "lazy" in the sense
        # that the client is never touched until a method actually needs it.
        return self.client_factory.iam()

    # -- Users ----------------------------------------------------------------

    def create_user(self: Self, name: str, path: str, tags: list[dict[str, str]]) -> IamUser:
        """Create an IAM user."""
        with aws_error_boundary("iam", "CreateUser"):
            response = self._client().create_user(UserName=name, Path=path, Tags=tags)
        return IamUser.model_validate(response["User"])

    def get_user(self: Self, name: str) -> IamUser:
        """Fetch a single IAM user by name."""
        with aws_error_boundary("iam", "GetUser"):
            response = self._client().get_user(UserName=name)
        return IamUser.model_validate(response["User"])

    def list_users(self: Self, path_prefix: str | None) -> list[IamUser]:
        """List every IAM user, fully paginated.

        A record AWS itself would reject client-side today (e.g. a ``Path``
        missing its slashes, left over from before this app validated on
        create/update) is skipped with a warning instead of aborting the
        whole list -- the TUI's IAM screen calls this before it can show
        any menu, so one malformed user must never block every other one.
        """
        kwargs: dict[str, Any] = {"PathPrefix": path_prefix} if path_prefix else {}
        users: list[IamUser] = []
        with aws_error_boundary("iam", "ListUsers"):
            for page in self._client().get_paginator("list_users").paginate(**kwargs):
                for raw in page["Users"]:
                    try:
                        users.append(IamUser.model_validate(raw))
                    except ValidationError as exc:
                        _logger.warning(
                            "Omitiendo usuario IAM '%s' con datos inválidos: %s",
                            raw.get("UserName", "?"),
                            exc,
                        )
        return users

    def delete_user(self: Self, name: str) -> None:
        """Delete an IAM user."""
        with aws_error_boundary("iam", "DeleteUser"):
            self._client().delete_user(UserName=name)

    def update_user(self: Self, name: str, new_name: str | None, new_path: str | None) -> IamUser:
        """Rename a user and/or change its path."""
        kwargs: dict[str, Any] = {"UserName": name}
        if new_name is not None:
            kwargs["NewUserName"] = new_name
        if new_path is not None:
            kwargs["NewPath"] = new_path
        with aws_error_boundary("iam", "UpdateUser"):
            self._client().update_user(**kwargs)
        return self.get_user(new_name if new_name is not None else name)

    def tag_user(self: Self, name: str, tags: dict[str, str]) -> None:
        """Add tags to a user, overwriting any existing tag that shares a key."""
        if not tags:
            return
        tag_list = [{"Key": key, "Value": value} for key, value in tags.items()]
        with aws_error_boundary("iam", "TagUser"):
            self._client().tag_user(UserName=name, Tags=tag_list)

    def untag_user(self: Self, name: str, keys: list[str]) -> None:
        """Remove tags by key from a user."""
        if not keys:
            return
        with aws_error_boundary("iam", "UntagUser"):
            self._client().untag_user(UserName=name, TagKeys=keys)

    # -- Groups -----------------------------------------------------------------

    def create_group(self: Self, name: str, path: str) -> IamGroup:
        """Create an IAM group."""
        with aws_error_boundary("iam", "CreateGroup"):
            response = self._client().create_group(GroupName=name, Path=path)
        return IamGroup.model_validate(response["Group"])

    def list_groups(self: Self, path_prefix: str | None) -> list[IamGroup]:
        """List every IAM group, fully paginated."""
        kwargs: dict[str, Any] = {"PathPrefix": path_prefix} if path_prefix else {}
        groups: list[IamGroup] = []
        with aws_error_boundary("iam", "ListGroups"):
            for page in self._client().get_paginator("list_groups").paginate(**kwargs):
                groups.extend(IamGroup.model_validate(raw) for raw in page["Groups"])
        return groups

    def delete_group(self: Self, name: str) -> None:
        """Delete an IAM group."""
        with aws_error_boundary("iam", "DeleteGroup"):
            self._client().delete_group(GroupName=name)

    def add_user_to_group(self: Self, group_name: str, user_name: str) -> None:
        """Add a user to a group."""
        with aws_error_boundary("iam", "AddUserToGroup"):
            self._client().add_user_to_group(GroupName=group_name, UserName=user_name)

    def remove_user_from_group(self: Self, group_name: str, user_name: str) -> None:
        """Remove a user from a group."""
        with aws_error_boundary("iam", "RemoveUserFromGroup"):
            self._client().remove_user_from_group(GroupName=group_name, UserName=user_name)

    def list_groups_for_user(self: Self, user_name: str) -> list[IamGroup]:
        """List every group a user belongs to, fully paginated."""
        groups: list[IamGroup] = []
        with aws_error_boundary("iam", "ListGroupsForUser"):
            paginator = self._client().get_paginator("list_groups_for_user")
            for page in paginator.paginate(UserName=user_name):
                groups.extend(IamGroup.model_validate(raw) for raw in page["Groups"])
        return groups

    def get_group_members(self: Self, name: str) -> list[IamUser]:
        """List every user that belongs to a group, fully paginated."""
        users: list[IamUser] = []
        with aws_error_boundary("iam", "GetGroup"):
            paginator = self._client().get_paginator("get_group")
            for page in paginator.paginate(GroupName=name):
                users.extend(IamUser.model_validate(raw) for raw in page["Users"])
        return users

    # -- Console (login profile) and programmatic (access key) access -------------

    def create_login_profile(
        self: Self, user_name: str, password: str, *, password_reset_required: bool
    ) -> LoginProfile:
        """Grant a user console access by setting an initial password."""
        with aws_error_boundary("iam", "CreateLoginProfile"):
            response = self._client().create_login_profile(
                UserName=user_name,
                Password=password,
                PasswordResetRequired=password_reset_required,
            )
        return LoginProfile.model_validate(response["LoginProfile"])

    def update_login_profile(
        self: Self,
        user_name: str,
        password: str | None,
        *,
        password_reset_required: bool | None,
    ) -> LoginProfile:
        """Update a user's existing console password and/or reset-required flag."""
        kwargs: dict[str, Any] = {"UserName": user_name}
        if password is not None:
            kwargs["Password"] = password
        if password_reset_required is not None:
            kwargs["PasswordResetRequired"] = password_reset_required
        with aws_error_boundary("iam", "UpdateLoginProfile"):
            self._client().update_login_profile(**kwargs)
        # UpdateLoginProfile's own response carries no body to hydrate from --
        # re-fetch. The profile we just updated cannot have vanished in between.
        profile = self.get_login_profile(user_name)
        assert profile is not None, "UpdateLoginProfile succeeded but GetLoginProfile found none"
        return profile

    def get_login_profile(self: Self, user_name: str) -> LoginProfile | None:
        """Fetch a user's login profile, or ``None`` if they have no console access."""
        try:
            with aws_error_boundary("iam", "GetLoginProfile"):
                response = self._client().get_login_profile(UserName=user_name)
        except ResourceNotFoundError:
            return None
        return LoginProfile.model_validate(response["LoginProfile"])

    def delete_login_profile(self: Self, user_name: str) -> None:
        """Revoke a user's console access entirely."""
        with aws_error_boundary("iam", "DeleteLoginProfile"):
            self._client().delete_login_profile(UserName=user_name)

    def create_access_key(self: Self, user_name: str) -> AccessKey:
        """Create a new access key for a user. The secret is returned ONLY here."""
        with aws_error_boundary("iam", "CreateAccessKey"):
            response = self._client().create_access_key(UserName=user_name)
        return AccessKey.model_validate(response["AccessKey"])

    def list_access_keys(self: Self, user_name: str) -> list[AccessKeyMetadata]:
        """List a user's access keys, fully paginated."""
        keys: list[AccessKeyMetadata] = []
        with aws_error_boundary("iam", "ListAccessKeys"):
            paginator = self._client().get_paginator("list_access_keys")
            for page in paginator.paginate(UserName=user_name):
                keys.extend(
                    AccessKeyMetadata.model_validate(raw) for raw in page["AccessKeyMetadata"]
                )
        return keys

    def update_access_key(self: Self, user_name: str, access_key_id: str, *, active: bool) -> None:
        """Activate or deactivate an access key, without deleting it."""
        status = "Active" if active else "Inactive"
        with aws_error_boundary("iam", "UpdateAccessKey"):
            self._client().update_access_key(
                UserName=user_name, AccessKeyId=access_key_id, Status=status
            )

    def delete_access_key(self: Self, user_name: str, access_key_id: str) -> None:
        """Permanently delete an access key."""
        with aws_error_boundary("iam", "DeleteAccessKey"):
            self._client().delete_access_key(UserName=user_name, AccessKeyId=access_key_id)

    def get_access_key_last_used(self: Self, access_key_id: str) -> datetime | None:
        """Fetch when ``access_key_id`` was last used, or ``None`` if never used.

        AWS omits ``LastUsedDate`` entirely from the response for a key
        that's never been used to sign a request -- there's no zero/sentinel
        timestamp to check against.
        """
        with aws_error_boundary("iam", "GetAccessKeyLastUsed"):
            response = self._client().get_access_key_last_used(AccessKeyId=access_key_id)
        last_used: dict[str, Any] = response.get("AccessKeyLastUsed", {})
        result: datetime | None = last_used.get("LastUsedDate")
        return result

    # -- MFA ----------------------------------------------------------------------

    def list_mfa_devices(self: Self, user_name: str) -> list[str]:
        """List the serial numbers of every MFA device enabled for a user, fully paginated."""
        serials: list[str] = []
        with aws_error_boundary("iam", "ListMFADevices"):
            paginator = self._client().get_paginator("list_mfa_devices")
            for page in paginator.paginate(UserName=user_name):
                serials.extend(device["SerialNumber"] for device in page["MFADevices"])
        return serials

    def deactivate_mfa_device(self: Self, user_name: str, serial_number: str) -> None:
        """Unlink an MFA device from a user; the device object itself is untouched."""
        with aws_error_boundary("iam", "DeactivateMFADevice"):
            self._client().deactivate_mfa_device(UserName=user_name, SerialNumber=serial_number)

    def delete_virtual_mfa_device(self: Self, serial_number: str) -> None:
        """Permanently delete a virtual MFA device object."""
        with aws_error_boundary("iam", "DeleteVirtualMFADevice"):
            self._client().delete_virtual_mfa_device(SerialNumber=serial_number)

    # -- Policies ---------------------------------------------------------------

    def create_policy(
        self: Self,
        name: str,
        document: PolicyDocument,
        path: str,
        description: str | None,
    ) -> IamPolicy:
        """Create a customer-managed IAM policy."""
        kwargs: dict[str, Any] = {
            "PolicyName": name,
            "PolicyDocument": document.to_aws_json(),
            "Path": path,
        }
        if description is not None:
            kwargs["Description"] = description
        with aws_error_boundary("iam", "CreatePolicy"):
            response = self._client().create_policy(**kwargs)
        return IamPolicy.model_validate(response["Policy"])

    def get_policy(self: Self, arn: str) -> IamPolicy:
        """Fetch a single IAM policy by ARN."""
        with aws_error_boundary("iam", "GetPolicy"):
            response = self._client().get_policy(PolicyArn=arn)
        return IamPolicy.model_validate(response["Policy"])

    def list_policies(self: Self, scope: Any, only_attached: bool) -> list[IamPolicy]:
        """List IAM policies, fully paginated."""
        policies: list[IamPolicy] = []
        with aws_error_boundary("iam", "ListPolicies"):
            paginator = self._client().get_paginator("list_policies")
            for page in paginator.paginate(Scope=scope, OnlyAttached=only_attached):
                policies.extend(IamPolicy.model_validate(raw) for raw in page["Policies"])
        return policies

    def delete_policy(self: Self, arn: str) -> None:
        """Delete a customer-managed IAM policy."""
        with aws_error_boundary("iam", "DeletePolicy"):
            self._client().delete_policy(PolicyArn=arn)

    def get_policy_document(self: Self, arn: str, version_id: str | None) -> PolicyDocument:
        """Fetch a policy's document, defaulting to its current default version."""
        client = self._client()
        if version_id is None:
            with aws_error_boundary("iam", "GetPolicy"):
                policy_response = client.get_policy(PolicyArn=arn)
            version_id = policy_response["Policy"]["DefaultVersionId"]

        with aws_error_boundary("iam", "GetPolicyVersion"):
            version_response = client.get_policy_version(PolicyArn=arn, VersionId=version_id)
        return PolicyDocument.from_aws(version_response["PolicyVersion"]["Document"])

    # -- Roles --------------------------------------------------------------------

    def create_role(
        self: Self,
        name: str,
        trust_policy: PolicyDocument,
        path: str,
        description: str | None,
        max_session_duration: int | None,
    ) -> IamRole:
        """Create an IAM role."""
        kwargs: dict[str, Any] = {
            "RoleName": name,
            "AssumeRolePolicyDocument": trust_policy.to_aws_json(),
            "Path": path,
        }
        if description is not None:
            kwargs["Description"] = description
        if max_session_duration is not None:
            kwargs["MaxSessionDuration"] = max_session_duration
        with aws_error_boundary("iam", "CreateRole"):
            response = self._client().create_role(**kwargs)
        return _hydrate_role(response["Role"])

    def get_role(self: Self, name: str) -> IamRole:
        """Fetch a single IAM role by name."""
        with aws_error_boundary("iam", "GetRole"):
            response = self._client().get_role(RoleName=name)
        return _hydrate_role(response["Role"])

    def list_roles(self: Self, path_prefix: str | None) -> list[IamRole]:
        """List every IAM role, fully paginated."""
        kwargs: dict[str, Any] = {"PathPrefix": path_prefix} if path_prefix else {}
        roles: list[IamRole] = []
        with aws_error_boundary("iam", "ListRoles"):
            for page in self._client().get_paginator("list_roles").paginate(**kwargs):
                roles.extend(_hydrate_role(raw) for raw in page["Roles"])
        return roles

    def delete_role(self: Self, name: str) -> None:
        """Delete an IAM role."""
        with aws_error_boundary("iam", "DeleteRole"):
            self._client().delete_role(RoleName=name)

    # -- Attachments ------------------------------------------------------------

    def attach_user_policy(self: Self, user_name: str, policy_arn: str) -> None:
        """Attach a managed policy to a user."""
        with aws_error_boundary("iam", "AttachUserPolicy"):
            self._client().attach_user_policy(UserName=user_name, PolicyArn=policy_arn)

    def detach_user_policy(self: Self, user_name: str, policy_arn: str) -> None:
        """Detach a managed policy from a user."""
        with aws_error_boundary("iam", "DetachUserPolicy"):
            self._client().detach_user_policy(UserName=user_name, PolicyArn=policy_arn)

    def list_attached_user_policies(self: Self, user_name: str) -> list[AttachedPolicy]:
        """List every policy attached to a user, fully paginated."""
        policies: list[AttachedPolicy] = []
        with aws_error_boundary("iam", "ListAttachedUserPolicies"):
            paginator = self._client().get_paginator("list_attached_user_policies")
            for page in paginator.paginate(UserName=user_name):
                policies.extend(
                    AttachedPolicy.model_validate(raw) for raw in page["AttachedPolicies"]
                )
        return policies

    def attach_role_policy(self: Self, role_name: str, policy_arn: str) -> None:
        """Attach a managed policy to a role."""
        with aws_error_boundary("iam", "AttachRolePolicy"):
            self._client().attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)

    def detach_role_policy(self: Self, role_name: str, policy_arn: str) -> None:
        """Detach a managed policy from a role."""
        with aws_error_boundary("iam", "DetachRolePolicy"):
            self._client().detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)

    def list_attached_role_policies(self: Self, role_name: str) -> list[AttachedPolicy]:
        """List every policy attached to a role, fully paginated."""
        policies: list[AttachedPolicy] = []
        with aws_error_boundary("iam", "ListAttachedRolePolicies"):
            paginator = self._client().get_paginator("list_attached_role_policies")
            for page in paginator.paginate(RoleName=role_name):
                policies.extend(
                    AttachedPolicy.model_validate(raw) for raw in page["AttachedPolicies"]
                )
        return policies

    # -- Instance profiles --------------------------------------------------------

    def create_instance_profile(self: Self, name: str, path: str) -> IamInstanceProfile:
        """Create an instance profile (with no role attached yet)."""
        with aws_error_boundary("iam", "CreateInstanceProfile"):
            response = self._client().create_instance_profile(InstanceProfileName=name, Path=path)
        return _hydrate_instance_profile(response["InstanceProfile"])

    def get_instance_profile(self: Self, name: str) -> IamInstanceProfile:
        """Fetch a single instance profile by name, including its attached role(s)."""
        with aws_error_boundary("iam", "GetInstanceProfile"):
            response = self._client().get_instance_profile(InstanceProfileName=name)
        return _hydrate_instance_profile(response["InstanceProfile"])

    def list_instance_profiles(self: Self, path_prefix: str | None) -> list[IamInstanceProfile]:
        """List every instance profile, fully paginated."""
        kwargs: dict[str, Any] = {"PathPrefix": path_prefix} if path_prefix else {}
        profiles: list[IamInstanceProfile] = []
        with aws_error_boundary("iam", "ListInstanceProfiles"):
            paginator = self._client().get_paginator("list_instance_profiles")
            for page in paginator.paginate(**kwargs):
                profiles.extend(_hydrate_instance_profile(raw) for raw in page["InstanceProfiles"])
        return profiles

    def delete_instance_profile(self: Self, name: str) -> None:
        """Delete an instance profile."""
        with aws_error_boundary("iam", "DeleteInstanceProfile"):
            self._client().delete_instance_profile(InstanceProfileName=name)

    def add_role_to_instance_profile(self: Self, profile_name: str, role_name: str) -> None:
        """Attach a role to an instance profile."""
        with aws_error_boundary("iam", "AddRoleToInstanceProfile"):
            self._client().add_role_to_instance_profile(
                InstanceProfileName=profile_name, RoleName=role_name
            )

    def remove_role_from_instance_profile(self: Self, profile_name: str, role_name: str) -> None:
        """Detach a role from an instance profile."""
        with aws_error_boundary("iam", "RemoveRoleFromInstanceProfile"):
            self._client().remove_role_from_instance_profile(
                InstanceProfileName=profile_name, RoleName=role_name
            )

    # -- Credential Report --------------------------------------------------------

    def get_credential_report(self: Self) -> bytes:
        """Generate (or reuse) the account's credential report, then fetch its CSV bytes.

        AWS's own two-call, eventually-consistent contract: ``GenerateCredentialReport``
        only starts/refreshes a background job -- the very next
        ``GetCredentialReport`` can still answer ``ReportInProgress`` while
        that job finishes, so this polls briefly rather than surfacing that
        transient state as an error.
        """
        client = self._client()
        with aws_error_boundary("iam", "GenerateCredentialReport"):
            client.generate_credential_report()
        last_exc: ClientError | None = None
        for _attempt in range(_CREDENTIAL_REPORT_MAX_ATTEMPTS):
            try:
                response = client.get_credential_report()
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ReportInProgress":
                    raise
                last_exc = exc
                time.sleep(_CREDENTIAL_REPORT_POLL_SECONDS)
                continue
            return cast(bytes, response["Content"])
        assert last_exc is not None  # the loop only exits early via `return`
        with aws_error_boundary("iam", "GetCredentialReport"):
            raise last_exc


if TYPE_CHECKING:
    # Static conformance check: mypy fails right here if Boto3IamGateway's
    # method signatures ever drift from the IamGateway Protocol.
    _iam_gateway_conformance: IamGateway = cast(Boto3IamGateway, None)

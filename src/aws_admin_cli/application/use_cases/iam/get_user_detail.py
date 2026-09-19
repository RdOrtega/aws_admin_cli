"""Use case: gather a user's full detail (groups, console access, keys, policies)."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import (
    AccessKeyMetadata,
    AttachedPolicy,
    IamGroup,
    IamUser,
    LoginProfile,
)
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class UserDetail:
    """A user's info, aggregated from several IAM calls -- not a single AWS response shape."""

    user: IamUser
    groups: list[IamGroup]
    login_profile: LoginProfile | None
    access_keys: list[AccessKeyMetadata]
    attached_policies: list[AttachedPolicy]
    mfa_devices: list[str]


@dataclass(frozen=True, slots=True)
class GetUserDetailUseCase:
    """Fetch a user's groups, console access, access keys, policies, and MFA in one call."""

    gateway: IamGateway

    def execute(self: Self, name: str) -> UserDetail:
        """Return the aggregated detail for user ``name``."""
        return UserDetail(
            user=self.gateway.get_user(name),
            groups=self.gateway.list_groups_for_user(name),
            login_profile=self.gateway.get_login_profile(name),
            access_keys=self.gateway.list_access_keys(name),
            attached_policies=self.gateway.list_attached_user_policies(name),
            mfa_devices=self.gateway.list_mfa_devices(name),
        )

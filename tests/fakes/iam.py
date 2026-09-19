"""In-memory ``IamGateway``/``Repository`` test doubles.

Used instead of ``Mock()`` on purpose: a fake that structurally implements the
real ports will break loudly (a ``TypeError``, or simply behaving wrong) the
moment a use case's assumptions about a port's contract drift, in a way an
unconstrained ``Mock()`` cannot -- and the tests that use them read like the
real call sequence a CLI command would produce.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from aws_admin_cli.core.exceptions import ResourceAlreadyExistsError, ResourceNotFoundError
from aws_admin_cli.domain.models.common import ResourceRecord
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


def _arn(kind: str, name: str) -> str:
    return f"arn:aws:iam::123456789012:{kind}/{name}"


@dataclass
class FakeIamGateway:
    """A structurally-typed ``IamGateway`` double, backed by plain dicts."""

    users: dict[str, IamUser] = field(default_factory=dict)
    policies: dict[str, IamPolicy] = field(default_factory=dict)
    policy_documents: dict[str, PolicyDocument] = field(default_factory=dict)
    roles: dict[str, IamRole] = field(default_factory=dict)
    user_policies: dict[str, set[str]] = field(default_factory=dict)
    role_policies: dict[str, set[str]] = field(default_factory=dict)
    instance_profiles: dict[str, IamInstanceProfile] = field(default_factory=dict)
    instance_profile_roles: dict[str, str] = field(default_factory=dict)
    groups: dict[str, IamGroup] = field(default_factory=dict)
    group_members: dict[str, set[str]] = field(default_factory=dict)
    login_profiles: dict[str, LoginProfile] = field(default_factory=dict)
    access_keys: dict[str, dict[str, AccessKeyMetadata]] = field(default_factory=dict)
    access_key_last_used: dict[str, datetime] = field(default_factory=dict)
    mfa_devices: dict[str, list[str]] = field(default_factory=dict)
    credential_report: bytes = field(
        default=b"user,arn,user_creation_time,password_enabled,password_last_used,"
        b"password_last_changed,password_next_rotation,mfa_active,access_key_1_active,"
        b"access_key_1_last_rotated,access_key_1_last_used_date,access_key_2_active,"
        b"access_key_2_last_rotated,access_key_2_last_used_date\n"
    )
    _next_access_key_number: int = field(default=1, init=False, repr=False)

    # -- Users ----------------------------------------------------------------

    def create_user(self, name: str, path: str, tags: list[dict[str, str]]) -> IamUser:
        if name in self.users:
            raise ResourceAlreadyExistsError(
                f"El usuario '{name}' ya existe.", aws_code="EntityAlreadyExists"
            )
        user = IamUser(
            user_name=name,
            user_id=f"AID{name}",
            arn=_arn("user", name),
            path=path,
            create_date=datetime.now(UTC),
            tags=tags,
        )
        self.users[name] = user
        self.user_policies.setdefault(name, set())
        return user

    def get_user(self, name: str) -> IamUser:
        user = self.users.get(name)
        if user is None:
            raise ResourceNotFoundError(f"El usuario '{name}' no existe.", aws_code="NoSuchEntity")
        return user

    def list_users(self, path_prefix: str | None) -> list[IamUser]:
        users = list(self.users.values())
        if path_prefix:
            users = [u for u in users if u.path.startswith(path_prefix)]
        return users

    def delete_user(self, name: str) -> None:
        if name not in self.users:
            raise ResourceNotFoundError(f"El usuario '{name}' no existe.", aws_code="NoSuchEntity")
        del self.users[name]
        self.user_policies.pop(name, None)

    def update_user(self, name: str, new_name: str | None, new_path: str | None) -> IamUser:
        user = self.get_user(name)
        updated = user.model_copy(
            update={
                "user_name": new_name if new_name is not None else user.user_name,
                "path": new_path if new_path is not None else user.path,
            }
        )
        if new_name is not None and new_name != name:
            del self.users[name]
            self.user_policies[new_name] = self.user_policies.pop(name, set())
            for members in self.group_members.values():
                if name in members:
                    members.discard(name)
                    members.add(new_name)
        self.users[updated.user_name] = updated
        return updated

    def tag_user(self, name: str, tags: dict[str, str]) -> None:
        user = self.get_user(name)
        merged = {tag["Key"]: tag["Value"] for tag in user.tags}
        merged.update(tags)
        self.users[name] = user.model_copy(
            update={"tags": [{"Key": k, "Value": v} for k, v in merged.items()]}
        )

    def untag_user(self, name: str, keys: list[str]) -> None:
        user = self.get_user(name)
        remaining = {tag["Key"]: tag["Value"] for tag in user.tags if tag["Key"] not in keys}
        self.users[name] = user.model_copy(
            update={"tags": [{"Key": k, "Value": v} for k, v in remaining.items()]}
        )

    # -- Groups ---------------------------------------------------------------

    def create_group(self, name: str, path: str) -> IamGroup:
        if name in self.groups:
            raise ResourceAlreadyExistsError(
                f"El grupo '{name}' ya existe.", aws_code="EntityAlreadyExists"
            )
        group = IamGroup(
            group_name=name,
            group_id=f"AGP{name}",
            arn=_arn("group", name),
            path=path,
            create_date=datetime.now(UTC),
        )
        self.groups[name] = group
        self.group_members.setdefault(name, set())
        return group

    def list_groups(self, path_prefix: str | None) -> list[IamGroup]:
        groups = list(self.groups.values())
        if path_prefix:
            groups = [g for g in groups if g.path.startswith(path_prefix)]
        return groups

    def delete_group(self, name: str) -> None:
        if name not in self.groups:
            raise ResourceNotFoundError(f"El grupo '{name}' no existe.", aws_code="NoSuchEntity")
        del self.groups[name]
        self.group_members.pop(name, None)

    def add_user_to_group(self, group_name: str, user_name: str) -> None:
        if group_name not in self.groups:
            raise ResourceNotFoundError(
                f"El grupo '{group_name}' no existe.", aws_code="NoSuchEntity"
            )
        if user_name not in self.users:
            raise ResourceNotFoundError(
                f"El usuario '{user_name}' no existe.", aws_code="NoSuchEntity"
            )
        self.group_members.setdefault(group_name, set()).add(user_name)

    def remove_user_from_group(self, group_name: str, user_name: str) -> None:
        self.group_members.get(group_name, set()).discard(user_name)

    def list_groups_for_user(self, user_name: str) -> list[IamGroup]:
        return [
            group
            for name, group in self.groups.items()
            if user_name in self.group_members.get(name, set())
        ]

    def get_group_members(self, name: str) -> list[IamUser]:
        member_names = self.group_members.get(name, set())
        return [self.users[n] for n in member_names if n in self.users]

    # -- Console (login profile) and programmatic (access key) access -------------

    def create_login_profile(
        self, user_name: str, password: str, *, password_reset_required: bool
    ) -> LoginProfile:
        del password
        if user_name in self.login_profiles:
            raise ResourceAlreadyExistsError(
                f"El usuario '{user_name}' ya tiene acceso a consola.",
                aws_code="EntityAlreadyExists",
            )
        profile = LoginProfile(
            user_name=user_name,
            create_date=datetime.now(UTC),
            password_reset_required=password_reset_required,
        )
        self.login_profiles[user_name] = profile
        return profile

    def update_login_profile(
        self,
        user_name: str,
        password: str | None,
        *,
        password_reset_required: bool | None,
    ) -> LoginProfile:
        del password
        existing = self.login_profiles.get(user_name)
        if existing is None:
            raise ResourceNotFoundError(
                f"El usuario '{user_name}' no tiene acceso a consola.", aws_code="NoSuchEntity"
            )
        updated = existing.model_copy(
            update={
                "password_reset_required": (
                    password_reset_required
                    if password_reset_required is not None
                    else existing.password_reset_required
                )
            }
        )
        self.login_profiles[user_name] = updated
        return updated

    def get_login_profile(self, user_name: str) -> LoginProfile | None:
        return self.login_profiles.get(user_name)

    def delete_login_profile(self, user_name: str) -> None:
        if user_name not in self.login_profiles:
            raise ResourceNotFoundError(
                f"El usuario '{user_name}' no tiene acceso a consola.", aws_code="NoSuchEntity"
            )
        del self.login_profiles[user_name]

    def create_access_key(self, user_name: str) -> AccessKey:
        key_id = f"AKIA{self._next_access_key_number:016d}"
        self._next_access_key_number += 1
        now = datetime.now(UTC)
        self.access_keys.setdefault(user_name, {})[key_id] = AccessKeyMetadata(
            access_key_id=key_id, status="Active", create_date=now
        )
        return AccessKey(
            access_key_id=key_id,
            secret_access_key=f"FAKE-SECRET-{key_id}",
            status="Active",
            create_date=now,
        )

    def list_access_keys(self, user_name: str) -> list[AccessKeyMetadata]:
        return list(self.access_keys.get(user_name, {}).values())

    def update_access_key(self, user_name: str, access_key_id: str, *, active: bool) -> None:
        keys = self.access_keys.get(user_name, {})
        existing = keys.get(access_key_id)
        if existing is None:
            raise ResourceNotFoundError(
                f"La access key '{access_key_id}' no existe.", aws_code="NoSuchEntity"
            )
        keys[access_key_id] = existing.model_copy(
            update={"status": "Active" if active else "Inactive"}
        )

    def delete_access_key(self, user_name: str, access_key_id: str) -> None:
        keys = self.access_keys.get(user_name, {})
        if access_key_id not in keys:
            raise ResourceNotFoundError(
                f"La access key '{access_key_id}' no existe.", aws_code="NoSuchEntity"
            )
        del keys[access_key_id]

    def get_access_key_last_used(self, access_key_id: str) -> datetime | None:
        return self.access_key_last_used.get(access_key_id)

    # -- MFA ------------------------------------------------------------------

    def list_mfa_devices(self, user_name: str) -> list[str]:
        return list(self.mfa_devices.get(user_name, []))

    # -- Policies -----------------------------------------------------------

    def create_policy(
        self, name: str, document: PolicyDocument, path: str, description: str | None
    ) -> IamPolicy:
        arn = _arn("policy", name)
        if arn in self.policies:
            raise ResourceAlreadyExistsError(
                f"La política '{name}' ya existe.", aws_code="EntityAlreadyExists"
            )
        policy = IamPolicy(
            policy_name=name,
            policy_id=f"POL{name}",
            arn=arn,
            path=path,
            default_version_id="v1",
            attachment_count=0,
            description=description,
            create_date=datetime.now(UTC),
        )
        self.policies[arn] = policy
        self.policy_documents[arn] = document
        return policy

    def get_policy(self, arn: str) -> IamPolicy:
        policy = self.policies.get(arn)
        if policy is None:
            raise ResourceNotFoundError(f"La política '{arn}' no existe.", aws_code="NoSuchEntity")
        return policy.model_copy(update={"attachment_count": self._attachment_count(arn)})

    def list_policies(
        self, scope: Literal["All", "AWS", "Local"], only_attached: bool
    ) -> list[IamPolicy]:
        policies = [
            p.model_copy(update={"attachment_count": self._attachment_count(p.arn)})
            for p in self.policies.values()
        ]
        if only_attached:
            policies = [p for p in policies if p.attachment_count > 0]
        return policies

    def delete_policy(self, arn: str) -> None:
        if arn not in self.policies:
            raise ResourceNotFoundError(f"La política '{arn}' no existe.", aws_code="NoSuchEntity")
        del self.policies[arn]
        self.policy_documents.pop(arn, None)

    def get_policy_document(self, arn: str, version_id: str | None) -> PolicyDocument:
        document = self.policy_documents.get(arn)
        if document is None:
            raise ResourceNotFoundError(f"La política '{arn}' no existe.", aws_code="NoSuchEntity")
        return document

    def _attachment_count(self, arn: str) -> int:
        count = sum(1 for policies in self.user_policies.values() if arn in policies)
        count += sum(1 for policies in self.role_policies.values() if arn in policies)
        return count

    # -- Roles ----------------------------------------------------------------

    def create_role(
        self,
        name: str,
        trust_policy: PolicyDocument,
        path: str,
        description: str | None,
        max_session_duration: int | None,
    ) -> IamRole:
        if name in self.roles:
            raise ResourceAlreadyExistsError(
                f"El rol '{name}' ya existe.", aws_code="EntityAlreadyExists"
            )
        role = IamRole(
            role_name=name,
            role_id=f"ROL{name}",
            arn=_arn("role", name),
            path=path,
            assume_role_policy_document=trust_policy,
            description=description,
            max_session_duration=max_session_duration,
            create_date=datetime.now(UTC),
        )
        self.roles[name] = role
        self.role_policies.setdefault(name, set())
        return role

    def get_role(self, name: str) -> IamRole:
        role = self.roles.get(name)
        if role is None:
            raise ResourceNotFoundError(f"El rol '{name}' no existe.", aws_code="NoSuchEntity")
        return role

    def list_roles(self, path_prefix: str | None) -> list[IamRole]:
        roles = list(self.roles.values())
        if path_prefix:
            roles = [r for r in roles if r.path.startswith(path_prefix)]
        return roles

    def delete_role(self, name: str) -> None:
        if name not in self.roles:
            raise ResourceNotFoundError(f"El rol '{name}' no existe.", aws_code="NoSuchEntity")
        del self.roles[name]
        self.role_policies.pop(name, None)

    # -- Attachments ------------------------------------------------------------

    def attach_user_policy(self, user_name: str, policy_arn: str) -> None:
        if user_name not in self.users:
            raise ResourceNotFoundError(
                f"El usuario '{user_name}' no existe.", aws_code="NoSuchEntity"
            )
        self.user_policies.setdefault(user_name, set()).add(policy_arn)

    def detach_user_policy(self, user_name: str, policy_arn: str) -> None:
        self.user_policies.get(user_name, set()).discard(policy_arn)

    def list_attached_user_policies(self, user_name: str) -> list[AttachedPolicy]:
        arns = self.user_policies.get(user_name, set())
        return [
            AttachedPolicy(policy_name=self.policies[arn].policy_name, policy_arn=arn)
            for arn in arns
            if arn in self.policies
        ]

    def attach_role_policy(self, role_name: str, policy_arn: str) -> None:
        if role_name not in self.roles:
            raise ResourceNotFoundError(f"El rol '{role_name}' no existe.", aws_code="NoSuchEntity")
        self.role_policies.setdefault(role_name, set()).add(policy_arn)

    def detach_role_policy(self, role_name: str, policy_arn: str) -> None:
        self.role_policies.get(role_name, set()).discard(policy_arn)

    def list_attached_role_policies(self, role_name: str) -> list[AttachedPolicy]:
        arns = self.role_policies.get(role_name, set())
        return [
            AttachedPolicy(policy_name=self.policies[arn].policy_name, policy_arn=arn)
            for arn in arns
            if arn in self.policies
        ]

    # -- Instance profiles --------------------------------------------------------

    def _hydrate_instance_profile(self, name: str) -> IamInstanceProfile:
        base = self.instance_profiles[name]
        role_name = self.instance_profile_roles.get(name)
        roles = [self.roles[role_name]] if role_name and role_name in self.roles else []
        return base.model_copy(update={"roles": roles})

    def create_instance_profile(self, name: str, path: str) -> IamInstanceProfile:
        if name in self.instance_profiles:
            raise ResourceAlreadyExistsError(
                f"El instance profile '{name}' ya existe.", aws_code="EntityAlreadyExists"
            )
        profile = IamInstanceProfile(
            instance_profile_name=name,
            instance_profile_id=f"AIPA{name}",
            arn=_arn("instance-profile", name),
            path=path,
            create_date=datetime.now(UTC),
            roles=[],
        )
        self.instance_profiles[name] = profile
        return profile

    def get_instance_profile(self, name: str) -> IamInstanceProfile:
        if name not in self.instance_profiles:
            raise ResourceNotFoundError(
                f"El instance profile '{name}' no existe.", aws_code="NoSuchEntity"
            )
        return self._hydrate_instance_profile(name)

    def list_instance_profiles(self, path_prefix: str | None) -> list[IamInstanceProfile]:
        names = list(self.instance_profiles)
        profiles = [self._hydrate_instance_profile(name) for name in names]
        if path_prefix:
            profiles = [p for p in profiles if p.path.startswith(path_prefix)]
        return profiles

    def delete_instance_profile(self, name: str) -> None:
        if name not in self.instance_profiles:
            raise ResourceNotFoundError(
                f"El instance profile '{name}' no existe.", aws_code="NoSuchEntity"
            )
        del self.instance_profiles[name]
        self.instance_profile_roles.pop(name, None)

    def add_role_to_instance_profile(self, profile_name: str, role_name: str) -> None:
        if profile_name not in self.instance_profiles:
            raise ResourceNotFoundError(
                f"El instance profile '{profile_name}' no existe.", aws_code="NoSuchEntity"
            )
        if role_name not in self.roles:
            raise ResourceNotFoundError(f"El rol '{role_name}' no existe.", aws_code="NoSuchEntity")
        self.instance_profile_roles[profile_name] = role_name

    def remove_role_from_instance_profile(self, profile_name: str, role_name: str) -> None:
        if self.instance_profile_roles.get(profile_name) == role_name:
            del self.instance_profile_roles[profile_name]

    # -- Credential Report --------------------------------------------------------

    def get_credential_report(self) -> bytes:
        return self.credential_report


@dataclass
class InMemoryRepository:
    """A ``Repository[ResourceRecord]`` double backed by a plain dict."""

    items: dict[str, ResourceRecord] = field(default_factory=dict)

    def save(self, item: ResourceRecord) -> None:
        self.items[item.key] = item

    def get(self, key: str) -> ResourceRecord | None:
        return self.items.get(key)

    def list_all(self) -> list[ResourceRecord]:
        return list(self.items.values())

    def delete(self, key: str) -> bool:
        if key not in self.items:
            return False
        del self.items[key]
        return True

    def exists(self, key: str) -> bool:
        return key in self.items

"""IAM domain models.

Every model hydrates directly from a raw boto3 response dict via
``Model.model_validate(response["User"])`` -- fields use a PascalCase alias
generator matching AWS's own casing, with ``populate_by_name=True`` so
constructing a model with our own snake_case kwargs (e.g. in tests, or when
building a value we're about to send to AWS) works too. ``extra="ignore"``
because AWS routinely adds response fields we don't model (e.g.
``RoleLastUsed``, ``Tags``, ``PermissionsBoundary``).

Name/path validators raise this project's own domain ``ValidationError``
(``aws_admin_cli.core.exceptions.ValidationError``), never Pydantic's, so a
malformed name fails the same way -- with the same message shape, hint, and
exit code (64) -- whether it was rejected by us before any network call or by
AWS itself.

``PolicyDocument``, ``PolicyStatement``, and ``PolicyEffect`` used to live here
but moved to ``domain/models/policy.py`` once S3 bucket policies needed the
exact same model -- a policy document isn't an IAM-specific concept. They're
re-exported from here so existing ``from aws_admin_cli.domain.models.iam
import PolicyDocument``-style imports keep working unchanged.
"""

import re
from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_pascal

from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.policy import PolicyDocument, PolicyEffect, PolicyStatement

__all__ = [
    "AccessKey",
    "AccessKeyMetadata",
    "AttachedPolicy",
    "IamGroup",
    "IamInstanceProfile",
    "IamPolicy",
    "IamRole",
    "IamUser",
    "LoginProfile",
    "PolicyDocument",
    "PolicyEffect",
    "PolicyStatement",
    "sanitize_path",
    "sanitize_user_name",
    "service_trust_policy",
    "validate_path",
    "validate_resource_name",
]

_NAME_RE = re.compile(r"^[\w+=,.@-]{1,64}$")
_POLICY_NAME_RE = re.compile(r"^[\w+=,.@-]{1,128}$")
_MAX_PATH_LENGTH = 512

_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="ignore",
    populate_by_name=True,
    alias_generator=to_pascal,
)


def validate_resource_name(name: str, *, max_length: int, field_label: str = "name") -> str:
    r"""Validate an IAM user/role/policy name against AWS's own naming rules.

    Checked client-side so a bad name fails immediately, before any network
    call, with the same domain ``ValidationError`` AWS's own rejection would
    eventually produce anyway.

    Args:
        name: Candidate name.
        max_length: 64 for users/roles, 128 for policies (AWS's own limits).
        field_label: Used in the error message (e.g. "user_name").

    Returns:
        ``name`` unchanged, if valid.

    Raises:
        ValidationError: ``name`` is empty, too long, or has characters
            outside AWS's allowed set (``[\\w+=,.@-]``).
    """
    pattern = _NAME_RE if max_length == 64 else _POLICY_NAME_RE
    if not name or len(name) > max_length or not pattern.match(name):
        raise ValidationError(
            f"'{name}' no es un {field_label} válido para IAM.",
            hint=(
                f"Usa entre 1 y {max_length} caracteres alfanuméricos, " "o de entre + = , . @ -."
            ),
        )
    return name


def validate_path(path: str) -> str:
    """Validate an IAM path (e.g. ``"/"``, ``"/service-role/"``).

    Raises:
        ValidationError: ``path`` doesn't start and end with ``"/"``, or
            exceeds AWS's 512-character limit.
    """
    if not path.startswith("/") or not path.endswith("/") or len(path) > _MAX_PATH_LENGTH:
        raise ValidationError(
            f"'{path}' no es un path de IAM válido.",
            hint=f'Debe empezar y terminar con "/", máx {_MAX_PATH_LENGTH} caracteres.',
        )
    return path


def sanitize_user_name(name: str) -> str:
    r"""Replace spaces with underscores.

    The most common way a real name (e.g. "Harold Ortega", typed where an
    IAM identifier belongs) ends up rejected. Deliberately narrow: only
    spaces are auto-fixed. Any other character outside AWS's allowed set
    (``[\w+=,.@-]``) still fails ``validate_resource_name`` afterwards,
    rather than being silently guessed at -- auto-fixing is for the one
    mistake that's unambiguous to correct.
    """
    return name.replace(" ", "_")


def sanitize_path(path: str) -> str:
    """Wrap ``path`` in leading/trailing slashes if it's missing either.

    ``""`` (no path given) is returned unchanged -- callers treat a blank
    path as "use the default", not as a path to normalize.
    """
    if not path:
        return path
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path = path + "/"
    return path


def service_trust_policy(service: str) -> PolicyDocument:
    """Build the standard ``sts:AssumeRole`` trust policy for an AWS service principal.

    Args:
        service: A service principal, e.g. ``"ec2.amazonaws.com"``.

    Returns:
        A ``PolicyDocument`` with a single ``Allow sts:AssumeRole`` statement
        for that service principal -- ready to pass as a role's trust policy.
    """
    return PolicyDocument(
        statement=[
            PolicyStatement(
                effect=PolicyEffect.ALLOW,
                action=["sts:AssumeRole"],
                principal={"Service": service},
            )
        ]
    )


class IamUser(BaseModel):
    """An IAM user, hydrated from a boto3 IAM ``User`` response."""

    model_config = _MODEL_CONFIG

    user_name: str
    user_id: str
    arn: str
    path: str
    create_date: datetime
    tags: list[dict[str, Any]] = Field(default_factory=list)
    # Absent from AWS's response for a user who has never signed in to the console --
    # console-only, deliberately: mirrors what ``ListUsers``/``GetUser`` return natively,
    # with no extra ``GetAccessKeyLastUsed`` calls per key.
    password_last_used: datetime | None = None

    @field_validator("user_name")
    @classmethod
    def _validate_user_name(cls: type[Self], value: str) -> str:
        return validate_resource_name(value, max_length=64, field_label="user_name")

    @field_validator("path")
    @classmethod
    def _validate_path(cls: type[Self], value: str) -> str:
        return validate_path(value)


class IamPolicy(BaseModel):
    """An IAM managed policy, hydrated from a boto3 IAM ``Policy`` response."""

    model_config = _MODEL_CONFIG

    policy_name: str
    policy_id: str
    arn: str
    path: str
    default_version_id: str
    attachment_count: int = 0
    description: str | None = None
    create_date: datetime

    @field_validator("policy_name")
    @classmethod
    def _validate_policy_name(cls: type[Self], value: str) -> str:
        return validate_resource_name(value, max_length=128, field_label="policy_name")

    @field_validator("path")
    @classmethod
    def _validate_path(cls: type[Self], value: str) -> str:
        return validate_path(value)


class IamRole(BaseModel):
    """An IAM role, hydrated from a boto3 IAM ``Role`` response."""

    model_config = _MODEL_CONFIG

    role_name: str
    role_id: str
    arn: str
    path: str
    assume_role_policy_document: PolicyDocument | None = None
    description: str | None = None
    max_session_duration: int | None = None
    create_date: datetime

    @field_validator("role_name")
    @classmethod
    def _validate_role_name(cls: type[Self], value: str) -> str:
        return validate_resource_name(value, max_length=64, field_label="role_name")

    @field_validator("path")
    @classmethod
    def _validate_path(cls: type[Self], value: str) -> str:
        return validate_path(value)


class AttachedPolicy(BaseModel):
    """A policy attached to a user or role, as returned by ``List*AttachedPolicies``."""

    model_config = _MODEL_CONFIG

    policy_name: str
    policy_arn: str


class IamInstanceProfile(BaseModel):
    """An IAM instance profile: the container an EC2 instance actually attaches.

    A role can't be attached to an instance directly -- EC2 attaches an
    *instance profile*, which wraps (in practice) exactly one role. This is
    the prerequisite Fase 5 (EC2) needs: ``ec2 instance launch --iam-role``
    resolves to one of these via
    ``application/use_cases/iam/ensure_instance_profile_for_role.py``, not
    directly to an ``IamRole``.
    """

    model_config = _MODEL_CONFIG

    instance_profile_name: str
    instance_profile_id: str
    arn: str
    path: str
    create_date: datetime
    roles: list[IamRole] = Field(default_factory=list)

    @field_validator("instance_profile_name")
    @classmethod
    def _validate_instance_profile_name(cls: type[Self], value: str) -> str:
        return validate_resource_name(value, max_length=128, field_label="instance_profile_name")

    @field_validator("path")
    @classmethod
    def _validate_path(cls: type[Self], value: str) -> str:
        return validate_path(value)

    @property
    def role_name(self: Self) -> str | None:
        """The name of this profile's first (in practice: only) role, if it has one."""
        return self.roles[0].role_name if self.roles else None


class IamGroup(BaseModel):
    """An IAM group, hydrated from a boto3 IAM ``Group`` response."""

    model_config = _MODEL_CONFIG

    group_name: str
    group_id: str
    arn: str
    path: str
    create_date: datetime

    @field_validator("group_name")
    @classmethod
    def _validate_group_name(cls: type[Self], value: str) -> str:
        return validate_resource_name(value, max_length=128, field_label="group_name")

    @field_validator("path")
    @classmethod
    def _validate_path(cls: type[Self], value: str) -> str:
        return validate_path(value)


class LoginProfile(BaseModel):
    """A user's IAM console (password) access, from ``Create``/``GetLoginProfile``.

    Never carries the password itself -- AWS's own API never returns it back,
    only whether one is set (implicitly, by this model existing at all: see
    ``GetIamUserDetailUseCase``, which treats "no ``LoginProfile``" as "no
    console access" rather than modeling that as a field here) and whether
    the user must change it at next sign-in.
    """

    model_config = _MODEL_CONFIG

    user_name: str
    create_date: datetime
    password_reset_required: bool = False


class AccessKey(BaseModel):
    """A freshly created IAM access key -- the ONLY response that ever carries the secret.

    AWS shows ``secret_access_key`` exactly once, at creation; every other
    access-key API (``ListAccessKeys``, ``UpdateAccessKey``) returns
    ``AccessKeyMetadata`` instead, which has no secret to leak. Never log or
    persist this model beyond the single interaction that displays it.
    """

    model_config = _MODEL_CONFIG

    access_key_id: str
    secret_access_key: str
    status: str
    create_date: datetime


class AccessKeyMetadata(BaseModel):
    """An access key's metadata, from ``ListAccessKeys`` -- never the secret itself."""

    model_config = _MODEL_CONFIG

    access_key_id: str
    status: str
    create_date: datetime

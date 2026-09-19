"""The generic IAM-style policy document model, shared by IAM and S3.

Originally lived in ``domain/models/iam.py`` (Fase 2); moved here once S3
needed the exact same ``PolicyDocument`` shape for bucket policies (Fase 3) --
a policy document isn't an IAM-specific concept, it's the JSON grammar AWS
uses for IAM policies, S3 bucket policies, KMS key policies, and more.
``iam.py`` re-exports these three names, so existing imports from there keep
working unchanged.
"""

import json
from enum import Enum
from typing import Any, Self
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_pascal

_MODEL_CONFIG = ConfigDict(
    frozen=True,
    extra="ignore",
    populate_by_name=True,
    alias_generator=to_pascal,
)


class PolicyEffect(str, Enum):
    """The ``Effect`` of a policy statement."""

    ALLOW = "Allow"
    DENY = "Deny"


class PolicyStatement(BaseModel):
    """A single statement within a policy document.

    ``resource`` is optional because it doesn't apply to every statement:
    permission-policy statements (the kind IAM's ``create_policy`` or an S3
    bucket policy send) always carry a ``Resource``, but *trust*-policy
    statements (an IAM ``AssumeRolePolicyDocument``) carry a ``Principal``
    instead and have no ``Resource`` at all -- AWS rejects a trust statement
    that includes one. ``principal`` exists for that second case (and for S3
    bucket policies, which always carry one); it's absent from ordinary IAM
    permission statements.
    """

    model_config = _MODEL_CONFIG

    sid: str | None = None
    effect: PolicyEffect
    action: list[str]
    resource: list[str] | None = None
    principal: dict[str, Any] | str | None = None
    condition: dict[str, Any] | None = None

    @field_validator("action", "resource", mode="before")
    @classmethod
    def _normalize_single_value_to_list(cls: type[Self], value: object) -> object:
        """AWS accepts either a single string or a list; we always store a list."""
        if isinstance(value, str):
            return [value]
        return value


class PolicyDocument(BaseModel):
    """A full policy document (the JSON you'd hand to ``PutPolicy``, ``PutBucketPolicy``, ...)."""

    model_config = _MODEL_CONFIG

    version: str = "2012-10-17"
    statement: list[PolicyStatement] = Field(min_length=1)

    def to_aws_json(self: Self) -> str:
        """Serialize with PascalCase keys, no superfluous whitespace, dropping ``None``s."""
        return self.model_dump_json(by_alias=True, exclude_none=True)

    @classmethod
    def from_aws(cls: type[Self], raw: str | dict[str, Any]) -> Self:
        """Parse a policy document as AWS hands it back: a dict, or a JSON string.

        IAM's ``AssumeRolePolicyDocument`` (and some other document fields) come
        back URL-encoded on real AWS but as a plain dict under moto -- this
        accepts either, decoding only when the string doesn't already look like
        raw JSON.
        """
        if isinstance(raw, dict):
            return cls.model_validate(raw)
        text = raw if raw.lstrip().startswith("{") else unquote(raw)
        return cls.model_validate(json.loads(text))

    def has_full_wildcard(self: Self) -> bool:
        """Whether any ``Allow`` statement grants ``"*"`` action on ``"*"`` resource."""
        return any(
            statement.effect is PolicyEffect.ALLOW
            and "*" in statement.action
            and statement.resource is not None
            and "*" in statement.resource
            for statement in self.statement
        )

    def has_public_principal(self: Self) -> bool:
        """Whether any ``Allow`` statement grants ``Principal: "*"`` (or ``{"AWS": "*"}``).

        The S3-bucket-policy analogue of ``has_full_wildcard``: an IAM policy's
        danger is an over-broad *action*/*resource*; a resource policy's (S3
        bucket, KMS key, ...) danger is an over-broad *principal* -- anyone on
        the internet, unauthenticated.
        """
        return any(
            statement.effect is PolicyEffect.ALLOW and _is_public_principal(statement.principal)
            for statement in self.statement
        )


def _is_public_principal(principal: dict[str, Any] | str | None) -> bool:
    if principal == "*":
        return True
    if isinstance(principal, dict):
        return any(value == "*" for value in principal.values())
    return False

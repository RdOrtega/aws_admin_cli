"""Use case: list a user's IAM access keys (metadata only)."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import AccessKeyMetadata
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class ListAccessKeysUseCase:
    """List every access key a user has (never their secrets)."""

    gateway: IamGateway

    def execute(self: Self, user_name: str) -> list[AccessKeyMetadata]:
        """Return ``user_name``'s access keys."""
        return self.gateway.list_access_keys(user_name)

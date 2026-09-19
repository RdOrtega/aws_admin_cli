"""Use case: list IAM users, optionally filtered by path prefix."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import IamUser
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class ListUsersUseCase:
    """List IAM users. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: IamGateway

    def execute(self: Self, path_prefix: str | None = None) -> list[IamUser]:
        """Return every IAM user, optionally filtered by ``path_prefix``."""
        return self.gateway.list_users(path_prefix)

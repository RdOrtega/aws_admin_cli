"""Use case: list IAM roles, optionally filtered by path prefix."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import IamRole
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class ListRolesUseCase:
    """List IAM roles. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: IamGateway

    def execute(self: Self, path_prefix: str | None = None) -> list[IamRole]:
        """Return every IAM role, optionally filtered by ``path_prefix``."""
        return self.gateway.list_roles(path_prefix)

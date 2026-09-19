"""Use case: list IAM groups."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import IamGroup
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class ListGroupsUseCase:
    """List every IAM group, optionally filtered by path prefix."""

    gateway: IamGateway

    def execute(self: Self, path_prefix: str | None) -> list[IamGroup]:
        """Return every group, or those under ``path_prefix`` if given."""
        return self.gateway.list_groups(path_prefix)

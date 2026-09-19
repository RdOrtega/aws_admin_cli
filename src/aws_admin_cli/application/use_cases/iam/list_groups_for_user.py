"""Use case: list the groups a user belongs to."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import IamGroup
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class ListGroupsForUserUseCase:
    """List every group ``user_name`` belongs to."""

    gateway: IamGateway

    def execute(self: Self, user_name: str) -> list[IamGroup]:
        """Return the groups ``user_name`` belongs to."""
        return self.gateway.list_groups_for_user(user_name)

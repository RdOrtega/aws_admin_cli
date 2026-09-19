"""Use case: remove a user from an IAM group."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import RemoveUserFromGroupRequest
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class RemoveUserFromGroupUseCase:
    """Remove a user from a group."""

    gateway: IamGateway

    def execute(self: Self, request: RemoveUserFromGroupRequest) -> None:
        """Remove ``request.user_name`` from ``request.group_name``."""
        self.gateway.remove_user_from_group(request.group_name, request.user_name)

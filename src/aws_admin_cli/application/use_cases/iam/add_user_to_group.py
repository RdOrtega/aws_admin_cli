"""Use case: add a user to an IAM group."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import AddUserToGroupRequest
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class AddUserToGroupUseCase:
    """Add a user to a group."""

    gateway: IamGateway

    def execute(self: Self, request: AddUserToGroupRequest) -> None:
        """Add ``request.user_name`` to ``request.group_name``."""
        self.gateway.add_user_to_group(request.group_name, request.user_name)

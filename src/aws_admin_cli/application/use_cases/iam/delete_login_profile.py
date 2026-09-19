"""Use case: revoke a user's IAM console access entirely."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DeleteLoginProfileRequest
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class DeleteLoginProfileUseCase:
    """Revoke a user's console access (delete their login profile)."""

    gateway: IamGateway

    def execute(self: Self, request: DeleteLoginProfileRequest) -> None:
        """Delete ``request.user_name``'s login profile."""
        self.gateway.delete_login_profile(request.user_name)

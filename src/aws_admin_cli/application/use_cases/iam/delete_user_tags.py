"""Use case: remove tags (by key) from an existing IAM user."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DeleteUserTagsRequest
from aws_admin_cli.domain.models.iam import IamUser
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class DeleteUserTagsUseCase:
    """Remove tags on a user. A no-op if ``request.keys`` is empty."""

    gateway: IamGateway

    def execute(self: Self, request: DeleteUserTagsRequest) -> IamUser:
        """Remove ``request.keys`` from ``request.name`` and return the refreshed user."""
        self.gateway.untag_user(request.name, list(request.keys))
        return self.gateway.get_user(request.name)

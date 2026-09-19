"""Use case: add or overwrite tags on an existing IAM user."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import SetUserTagsRequest
from aws_admin_cli.domain.models.iam import IamUser
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class SetUserTagsUseCase:
    """Add/update tags on a user. A no-op if ``request.tags`` is empty."""

    gateway: IamGateway

    def execute(self: Self, request: SetUserTagsRequest) -> IamUser:
        """Apply ``request.tags`` to ``request.name`` and return the refreshed user."""
        self.gateway.tag_user(request.name, request.tags)
        return self.gateway.get_user(request.name)

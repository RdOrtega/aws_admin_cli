"""Use case: activate or deactivate an IAM access key."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import UpdateAccessKeyRequest
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class UpdateAccessKeyUseCase:
    """Flip an access key's active/inactive status, without deleting it."""

    gateway: IamGateway

    def execute(self: Self, request: UpdateAccessKeyRequest) -> None:
        """Set ``request.access_key_id``'s status per ``request.active``."""
        self.gateway.update_access_key(
            request.user_name, request.access_key_id, active=request.active
        )

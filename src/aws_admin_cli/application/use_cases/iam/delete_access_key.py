"""Use case: permanently delete an IAM access key."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DeleteAccessKeyRequest
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class DeleteAccessKeyUseCase:
    """Delete an access key."""

    gateway: IamGateway

    def execute(self: Self, request: DeleteAccessKeyRequest) -> None:
        """Delete ``request.access_key_id`` from ``request.user_name``."""
        self.gateway.delete_access_key(request.user_name, request.access_key_id)

"""Use case: create a new IAM access key for a user."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import CreateAccessKeyRequest
from aws_admin_cli.domain.models.iam import AccessKey
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class CreateAccessKeyUseCase:
    """Create an access key. AWS returns the secret exactly once, in this response."""

    gateway: IamGateway

    def execute(self: Self, request: CreateAccessKeyRequest) -> AccessKey:
        """Create a new access key for ``request.user_name``."""
        return self.gateway.create_access_key(request.user_name)

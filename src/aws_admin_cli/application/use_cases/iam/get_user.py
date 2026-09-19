"""Use case: fetch a single IAM user by name."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import IamUser
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class GetUserUseCase:
    """Fetch a single IAM user. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: IamGateway

    def execute(self: Self, name: str) -> IamUser:
        """Return the IAM user named ``name``."""
        return self.gateway.get_user(name)

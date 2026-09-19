"""Use case: fetch a single IAM role by name."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import IamRole
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class GetRoleUseCase:
    """Fetch a single IAM role. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: IamGateway

    def execute(self: Self, name: str) -> IamRole:
        """Return the IAM role named ``name``."""
        return self.gateway.get_role(name)

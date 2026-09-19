"""Use case: list IAM instance profiles, optionally filtered by path prefix."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import IamInstanceProfile
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class ListInstanceProfilesUseCase:
    """List instance profiles. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: IamGateway

    def execute(self: Self, path_prefix: str | None = None) -> list[IamInstanceProfile]:
        """Return every instance profile, optionally filtered by ``path_prefix``."""
        return self.gateway.list_instance_profiles(path_prefix)

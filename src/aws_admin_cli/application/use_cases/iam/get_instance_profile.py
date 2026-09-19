"""Use case: fetch a single IAM instance profile by name."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import IamInstanceProfile
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class GetInstanceProfileUseCase:
    """Fetch a single instance profile. Kept as a use case so the CLI never touches the gateway."""

    gateway: IamGateway

    def execute(self: Self, name: str) -> IamInstanceProfile:
        """Return the instance profile named ``name``."""
        return self.gateway.get_instance_profile(name)

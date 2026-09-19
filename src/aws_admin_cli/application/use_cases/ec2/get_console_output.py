"""Use case: fetch an instance's console output (useful for debugging a failed boot)."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class GetConsoleOutputUseCase:
    """Fetch console output. Kept as a use case so the CLI never touches the gateway."""

    gateway: Ec2Gateway

    def execute(self: Self, instance_id: str) -> str:
        """Return ``instance_id``'s console output (empty string if nothing captured yet)."""
        return self.gateway.get_console_output(instance_id)

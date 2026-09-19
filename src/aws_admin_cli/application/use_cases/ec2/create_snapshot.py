"""Use case: create an EBS snapshot of a volume."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class CreateSnapshotUseCase:
    """Snapshot one EBS volume -- e.g. as a safety net before terminating its instance."""

    gateway: Ec2Gateway

    def execute(self: Self, volume_id: str, description: str | None = None) -> str:
        """Create the snapshot; returns its ID."""
        return self.gateway.create_snapshot(volume_id, description)

"""Use case: list EC2 key pairs (metadata only -- never private material)."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.ec2 import KeyPairInfo
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class ListKeyPairsUseCase:
    """List key pairs. Kept as a use case so the CLI never touches the gateway directly."""

    gateway: Ec2Gateway

    def execute(self: Self) -> list[KeyPairInfo]:
        """Return every key pair's metadata."""
        return self.gateway.describe_key_pairs(None)

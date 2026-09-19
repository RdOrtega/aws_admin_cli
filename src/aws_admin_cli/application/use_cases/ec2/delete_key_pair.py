"""Use case: delete an EC2 key pair (the AWS-side record, not any local file)."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class DeleteKeyPairUseCase:
    """Delete a key pair from AWS. Never touches any local ``.pem`` file."""

    gateway: Ec2Gateway

    def execute(self: Self, key_name: str) -> None:
        """Delete ``key_name``."""
        self.gateway.delete_key_pair(key_name)

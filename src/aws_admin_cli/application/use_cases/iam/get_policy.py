"""Use case: fetch a single IAM policy by ARN."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import IamPolicy
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class GetPolicyUseCase:
    """Fetch a single IAM policy. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: IamGateway

    def execute(self: Self, arn: str) -> IamPolicy:
        """Return the IAM policy identified by ``arn``."""
        return self.gateway.get_policy(arn)

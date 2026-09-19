"""Use case: list IAM policies, filtered by scope and attachment."""

from dataclasses import dataclass
from typing import Literal, Self

from aws_admin_cli.domain.models.iam import IamPolicy
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class ListPoliciesUseCase:
    """List IAM policies. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: IamGateway

    def execute(
        self: Self,
        scope: Literal["All", "AWS", "Local"] = "All",
        only_attached: bool = False,
    ) -> list[IamPolicy]:
        """Return every IAM policy matching ``scope``/``only_attached``."""
        return self.gateway.list_policies(scope, only_attached)

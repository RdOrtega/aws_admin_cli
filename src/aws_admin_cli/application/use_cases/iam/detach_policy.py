"""Use case: detach a managed policy from a user or role."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DetachPolicyRequest
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class DetachPolicyUseCase:
    """Detach a managed policy from a user or role.

    Dispatches to the user- or role-flavored gateway call based on
    ``request.principal_type`` -- via a dict of bound methods, not a chain of
    ``if``/``elif``.
    """

    gateway: IamGateway

    def _dispatch(self: Self) -> dict[str, Callable[[str, str], None]]:
        return {
            "user": self.gateway.detach_user_policy,
            "role": self.gateway.detach_role_policy,
        }

    def execute(self: Self, request: DetachPolicyRequest) -> None:
        """Detach the policy."""
        detach = self._dispatch()[request.principal_type]
        detach(request.principal_name, request.policy_arn)

"""Use case: list the policies attached to a user or role."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Self

from aws_admin_cli.domain.models.iam import AttachedPolicy
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class ListAttachedPoliciesUseCase:
    """List policies attached to a user or role (``iam user/role policies NAME``)."""

    gateway: IamGateway

    def _dispatch(self: Self) -> dict[str, Callable[[str], list[AttachedPolicy]]]:
        return {
            "user": self.gateway.list_attached_user_policies,
            "role": self.gateway.list_attached_role_policies,
        }

    def execute(
        self: Self, principal_name: str, principal_type: Literal["user", "role"]
    ) -> list[AttachedPolicy]:
        """Return every policy attached to ``principal_name``."""
        return self._dispatch()[principal_type](principal_name)

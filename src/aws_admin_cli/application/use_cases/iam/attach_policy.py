"""Use case: attach a managed policy to a user or role, idempotently."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import AttachPolicyRequest
from aws_admin_cli.domain.models.iam import AttachedPolicy
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class AttachPolicyUseCase:
    """Attach a managed policy to a user or role.

    Dispatches to the user- or role-flavored gateway calls based on
    ``request.principal_type`` -- via a dict of bound methods, not a chain of
    ``if``/``elif``. Idempotent: attaching an already-attached policy is a
    logged no-op, not an error.
    """

    gateway: IamGateway
    logger: logging.Logger

    def _dispatch(
        self: Self,
    ) -> dict[str, tuple[Callable[[str], list[AttachedPolicy]], Callable[[str, str], None]]]:
        return {
            "user": (self.gateway.list_attached_user_policies, self.gateway.attach_user_policy),
            "role": (self.gateway.list_attached_role_policies, self.gateway.attach_role_policy),
        }

    def execute(self: Self, request: AttachPolicyRequest) -> None:
        """Attach the policy, or log and return if it's already attached."""
        list_attached, attach = self._dispatch()[request.principal_type]

        attached = list_attached(request.principal_name)
        already_attached = any(policy.policy_arn == request.policy_arn for policy in attached)
        if already_attached:
            self.logger.info(
                "La política %s ya estaba adjunta a %s '%s'; nada que hacer.",
                request.policy_arn,
                request.principal_type,
                request.principal_name,
            )
            return

        attach(request.principal_name, request.policy_arn)

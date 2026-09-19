"""Use case: resolve (creating once if missing) the shared "Disable User" deny-all policy.

IAM has no native per-user enabled/disabled flag -- attaching this single,
account-wide Deny-all policy to a user (and detaching it) is what actually
blocks their AWS access. Every caller that disables/re-enables a user (the
TUI's detail-screen toggle, the Dormant/Disabled audit views, and the
``iam seed-audit-users`` CLI command) goes through this one use case so the
policy's name/document is defined in exactly one place.
"""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import CreatePolicyRequest
from aws_admin_cli.application.use_cases.iam.create_policy import CreatePolicyUseCase
from aws_admin_cli.domain.models.iam import PolicyDocument, PolicyEffect, PolicyStatement
from aws_admin_cli.domain.ports.iam_gateway import IamGateway

DENY_ALL_POLICY_NAME = "aws-admin-cli-deny-all"


@dataclass(frozen=True, slots=True)
class ResolveDenyAllPolicyUseCase:
    """Return the shared deny-all policy's ARN, creating it on first use."""

    gateway: IamGateway
    create_policy: CreatePolicyUseCase

    def execute(self: Self) -> str:
        """Return the deny-all policy's ARN, creating it if this account has none yet."""
        existing = next(
            (
                policy
                for policy in self.gateway.list_policies("Local", False)
                if policy.policy_name == DENY_ALL_POLICY_NAME
            ),
            None,
        )
        if existing is not None:
            return existing.arn
        created = self.create_policy.execute(
            CreatePolicyRequest(
                name=DENY_ALL_POLICY_NAME,
                document=PolicyDocument(
                    statement=[
                        PolicyStatement(effect=PolicyEffect.DENY, action=["*"], resource=["*"])
                    ]
                ),
                description=(
                    'Managed by aws-admin-cli. Attached to a user by "Disable User" to '
                    'block all AWS access; detached by "Enable User".'
                ),
            )
        )
        return created.arn

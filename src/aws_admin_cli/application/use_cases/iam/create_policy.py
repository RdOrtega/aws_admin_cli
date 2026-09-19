"""Use case: create a customer-managed IAM policy, guarding against full-wildcard grants."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from aws_admin_cli.application.dto.iam import CreatePolicyRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.iam import IamPolicy
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository


@dataclass(frozen=True, slots=True)
class CreatePolicyUseCase:
    """Create a customer-managed IAM policy: blocks full-wildcard grants by default."""

    gateway: IamGateway
    repository: Repository[ResourceRecord]
    profile: str
    region: str

    def execute(self: Self, request: CreatePolicyRequest) -> IamPolicy:
        """Create the policy.

        Args:
            request: The create request.

        Returns:
            The created ``IamPolicy``.

        Raises:
            ValidationError: The document grants ``Allow "*"`` on ``Resource
                "*"`` (full administrative access) and ``allow_wildcard`` is
                ``False``. Nothing is created in this case.
        """
        if request.document.has_full_wildcard() and not request.allow_wildcard:
            raise ValidationError(
                f'La política \'{request.name}\' otorga Allow "*" sobre Resource "*" '
                "-- acceso administrativo total.",
                hint="Si es intencional, vuelve a intentarlo con --allow-wildcard.",
            )

        policy = self.gateway.create_policy(
            request.name, request.document, request.path, request.description
        )
        self.repository.save(
            ResourceRecord(
                resource_type="iam:policy",
                identifier=policy.arn,
                arn=policy.arn,
                profile=self.profile,
                region=self.region,
                created_at=datetime.now(UTC),
                metadata={"statement_count": len(request.document.statement)},
            )
        )
        return policy

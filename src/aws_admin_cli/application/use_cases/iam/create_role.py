"""Use case: create an IAM role, idempotently, validating the trust policy first."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from aws_admin_cli.application.dto.iam import CreateRoleRequest
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.iam import IamRole
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository


@dataclass(frozen=True, slots=True)
class CreateRoleUseCase:
    """Create an IAM role: validates the trust policy, idempotent, tracked in the ledger."""

    gateway: IamGateway
    repository: Repository[ResourceRecord]
    profile: str
    region: str

    def execute(self: Self, request: CreateRoleRequest) -> IamRole:
        """Create the role, or return the existing one if ``if_not_exists`` and it exists.

        Args:
            request: The create request.

        Returns:
            The created ``IamRole`` -- or, when ``if_not_exists`` is set and a
            role by that name already exists, that pre-existing ``IamRole``.

        Raises:
            ValidationError: The trust policy has no statement granting
                ``sts:AssumeRole`` -- a role created with one couldn't ever be
                assumed by anything.
        """
        if not any(
            "sts:AssumeRole" in statement.action for statement in request.trust_policy.statement
        ):
            raise ValidationError(
                f"El trust policy de '{request.name}' no tiene ningún statement con "
                "acción sts:AssumeRole.",
                hint="Un rol necesita al menos un statement Allow sts:AssumeRole para "
                "algún principal (usa --service, o revisa tu --trust-policy-file).",
            )

        if request.if_not_exists:
            existing = self._find_existing(request.name)
            if existing is not None:
                return existing

        role = self.gateway.create_role(
            request.name,
            request.trust_policy,
            request.path,
            request.description,
            request.max_session_duration,
        )
        self._track(role)
        return role

    def _find_existing(self: Self, name: str) -> IamRole | None:
        try:
            return self.gateway.get_role(name)
        except ResourceNotFoundError:
            return None

    def _track(self: Self, role: IamRole) -> None:
        self.repository.save(
            ResourceRecord(
                resource_type="iam:role",
                identifier=role.role_name,
                arn=role.arn,
                profile=self.profile,
                region=self.region,
                created_at=datetime.now(UTC),
                metadata={"path": role.path},
            )
        )

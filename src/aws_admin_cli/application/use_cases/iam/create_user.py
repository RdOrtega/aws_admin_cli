"""Use case: create an IAM user, idempotently, with automatic tagging and ledger tracking."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from aws_admin_cli.application.dto.iam import CreateUserRequest
from aws_admin_cli.core.exceptions import ResourceNotFoundError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.iam import (
    IamUser,
    sanitize_path,
    sanitize_user_name,
    validate_path,
    validate_resource_name,
)
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository

MANAGED_BY_TAG_KEY = "ManagedBy"
MANAGED_BY_TAG_VALUE = "aws-admin-cli"


@dataclass(frozen=True, slots=True)
class CreateUserUseCase:
    """Create an IAM user: idempotent, auto-tagged, and tracked in the local ledger."""

    gateway: IamGateway
    repository: Repository[ResourceRecord]
    profile: str
    region: str

    def execute(self: Self, request: CreateUserRequest) -> IamUser:
        """Create the user, or return the existing one if ``if_not_exists`` and it exists.

        ``request.name``/``request.path`` are sanitized before anything else
        (spaces -> underscores in the name; the path gets wrapped in ``/``)
        -- the single point every caller (TUI, CLI) goes through, so a name
        like "Harold Ortega" can never reach AWS/LocalStack un-sanitized and
        come back unreadable by this CLI's own stricter validation. Whatever
        doesn't come out valid after sanitizing still raises
        ``ValidationError``, exactly as before.

        Args:
            request: The create request.

        Returns:
            The created ``IamUser`` -- or, when ``if_not_exists`` is set and a
            user by that name already exists, that pre-existing ``IamUser``,
            unmodified (idempotency: this never errors on "already exists").
        """
        name = validate_resource_name(
            sanitize_user_name(request.name), max_length=64, field_label="name"
        )
        path = validate_path(sanitize_path(request.path) or "/")

        if request.if_not_exists:
            existing = self._find_existing(name)
            if existing is not None:
                return existing

        tags = dict(request.tags)
        tags.setdefault(MANAGED_BY_TAG_KEY, MANAGED_BY_TAG_VALUE)
        user = self.gateway.create_user(
            name,
            path,
            [{"Key": key, "Value": value} for key, value in tags.items()],
        )
        self._track(user)
        return user

    def _find_existing(self: Self, name: str) -> IamUser | None:
        try:
            return self.gateway.get_user(name)
        except ResourceNotFoundError:
            return None

    def _track(self: Self, user: IamUser) -> None:
        self.repository.save(
            ResourceRecord(
                resource_type="iam:user",
                identifier=user.user_name,
                arn=user.arn,
                profile=self.profile,
                region=self.region,
                created_at=datetime.now(UTC),
                metadata={"path": user.path},
            )
        )

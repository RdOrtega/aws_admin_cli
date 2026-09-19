"""Use case: rename an IAM user and/or change its path."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import UpdateUserRequest
from aws_admin_cli.domain.models.iam import (
    IamUser,
    sanitize_path,
    sanitize_user_name,
    validate_path,
    validate_resource_name,
)
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class UpdateUserUseCase:
    """Rename a user and/or change its path. A no-op if neither is given."""

    gateway: IamGateway

    def execute(self: Self, request: UpdateUserRequest) -> IamUser:
        """Apply ``request.new_name``/``request.new_path`` to ``request.name``.

        Both are sanitized (spaces -> underscores; path wrapped in ``/``)
        before validation, same as ``CreateUserUseCase`` -- the single point
        every caller (TUI, CLI) goes through, so a rename can never leave a
        user unreadable by this CLI's own stricter validation.
        """
        new_name = (
            validate_resource_name(
                sanitize_user_name(request.new_name), max_length=64, field_label="new_name"
            )
            if request.new_name
            else None
        )
        new_path = validate_path(sanitize_path(request.new_path)) if request.new_path else None
        return self.gateway.update_user(request.name, new_name, new_path)

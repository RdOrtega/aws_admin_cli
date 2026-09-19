"""Use case: grant or update a user's IAM console (password) access."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import SetLoginProfileRequest
from aws_admin_cli.core.exceptions import ResourceAlreadyExistsError
from aws_admin_cli.domain.models.iam import LoginProfile
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class SetLoginProfileUseCase:
    """Upsert a user's console password: create it if absent, update it otherwise.

    The caller never needs to know which of the two applies -- ``IamGateway``
    itself is the only thing that knows (via ``ResourceAlreadyExistsError``),
    so this use case reacts to that rather than checking first (avoids a
    redundant ``GetLoginProfile`` call on the far more common "first time"
    path, and a check-then-act race either way).
    """

    gateway: IamGateway

    def execute(self: Self, request: SetLoginProfileRequest) -> LoginProfile:
        """Create or update ``request.user_name``'s login profile."""
        try:
            return self.gateway.create_login_profile(
                request.user_name,
                request.password,
                password_reset_required=request.password_reset_required,
            )
        except ResourceAlreadyExistsError:
            return self.gateway.update_login_profile(
                request.user_name,
                request.password,
                password_reset_required=request.password_reset_required,
            )

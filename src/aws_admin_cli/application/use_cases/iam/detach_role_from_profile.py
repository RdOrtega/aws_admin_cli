"""Use case: detach a role from an instance profile."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import AttachRoleToProfileRequest
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class DetachRoleFromProfileUseCase:
    """Detach a role from an instance profile."""

    gateway: IamGateway

    def execute(self: Self, request: AttachRoleToProfileRequest) -> None:
        """Detach ``request.role_name`` from ``request.profile_name``."""
        self.gateway.remove_role_from_instance_profile(request.profile_name, request.role_name)

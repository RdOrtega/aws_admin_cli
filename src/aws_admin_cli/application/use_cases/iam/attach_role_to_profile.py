"""Use case: attach a role to an instance profile, idempotently."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import AttachRoleToProfileRequest
from aws_admin_cli.domain.models.iam import IamInstanceProfile
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class AttachRoleToProfileUseCase:
    """Attach a role to an instance profile: a no-op if it's already attached.

    AWS itself rejects a second ``AddRoleToInstanceProfile`` for the same
    profile with ``LimitExceeded`` (a profile can only ever hold one role) --
    checking first, rather than reacting to that error, keeps this idempotent
    without depending on a specific AWS error code for "already attached".
    """

    gateway: IamGateway

    def execute(self: Self, request: AttachRoleToProfileRequest) -> IamInstanceProfile:
        """Attach ``request.role_name`` to ``request.profile_name``, or no-op if already attached.

        Returns:
            The instance profile's current state after the operation.
        """
        profile = self.gateway.get_instance_profile(request.profile_name)
        if profile.role_name == request.role_name:
            return profile

        self.gateway.add_role_to_instance_profile(request.profile_name, request.role_name)
        return self.gateway.get_instance_profile(request.profile_name)

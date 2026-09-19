"""Use case: delete an IAM instance profile, guarding against silently orphaning roles."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DeleteInstanceProfileRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository


@dataclass(frozen=True, slots=True)
class DeleteInstanceProfileUseCase:
    """Delete an instance profile: refuses to orphan an attached role unless ``force``."""

    gateway: IamGateway
    repository: Repository[ResourceRecord]

    def execute(self: Self, request: DeleteInstanceProfileRequest) -> None:
        """Delete ``request.name``.

        Raises:
            ValidationError: The profile still has a role attached and
                ``force`` is ``False``.
        """
        profile = self.gateway.get_instance_profile(request.name)
        if profile.roles and not request.force:
            raise ValidationError(
                f"El instance profile '{request.name}' tiene el rol "
                f"'{profile.role_name}' adjunto.",
                hint="Usa --force para desadjuntarlo primero, o hazlo manualmente con "
                "`iam instance-profile detach-role`.",
            )

        if request.force:
            for role in profile.roles:
                self.gateway.remove_role_from_instance_profile(request.name, role.role_name)

        self.gateway.delete_instance_profile(request.name)
        self.repository.delete(f"iam:instance-profile:{request.name}")

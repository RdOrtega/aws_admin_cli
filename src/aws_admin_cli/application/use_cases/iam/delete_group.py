"""Use case: delete an IAM group, refusing to strand members silently."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DeleteGroupRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class DeleteGroupUseCase:
    """Delete an IAM group: blocks on members unless ``force`` is set."""

    gateway: IamGateway

    def execute(self: Self, request: DeleteGroupRequest) -> None:
        """Delete the group.

        Raises:
            ValidationError: The group has members and ``force`` is
                ``False``. Nothing is deleted or removed in this case.
        """
        members = self.gateway.get_group_members(request.name)

        if members and not request.force:
            names = ", ".join(user.user_name for user in members)
            raise ValidationError(
                f"El grupo '{request.name}' tiene miembros: {names}.",
                hint="Usa --force para quitarlos del grupo y borrarlo de todas formas.",
            )

        for user in members:
            self.gateway.remove_user_from_group(request.name, user.user_name)

        self.gateway.delete_group(request.name)

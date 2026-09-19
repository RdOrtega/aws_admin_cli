"""Use case: delete an IAM role, refusing to strand attached policies silently."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DeleteRoleRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository


@dataclass(frozen=True, slots=True)
class DeleteRoleUseCase:
    """Delete an IAM role: blocks on attached policies unless ``force`` is set."""

    gateway: IamGateway
    repository: Repository[ResourceRecord]

    def execute(self: Self, request: DeleteRoleRequest) -> None:
        """Delete the role.

        Args:
            request: The delete request.

        Raises:
            ValidationError: The role has attached policies and ``force`` is
                ``False``. Nothing is deleted or detached in this case.
        """
        attached = self.gateway.list_attached_role_policies(request.name)

        if attached and not request.force:
            names = ", ".join(policy.policy_name for policy in attached)
            raise ValidationError(
                f"El rol '{request.name}' tiene políticas adjuntas: {names}.",
                hint="Usa --force para desadjuntarlas y borrar el rol de todas formas.",
            )

        for policy in attached:
            self.gateway.detach_role_policy(request.name, policy.policy_arn)

        self.gateway.delete_role(request.name)
        self.repository.delete(f"iam:role:{request.name}")

"""Use case: delete an IAM user, refusing to strand any attachment silently."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DeleteUserRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository


@dataclass(frozen=True, slots=True)
class DeleteUserUseCase:
    """Delete an IAM user: blocks on any attachment unless ``force`` is set.

    AWS's own ``DeleteUser`` rejects a user with ANY of the four attachments
    below still in place (``DeleteConflict``) -- not just managed policies.
    Checking all four up front means a blocked delete fails with one clear,
    actionable message instead of the caller retrying four times against
    four different opaque AWS errors.
    """

    gateway: IamGateway
    repository: Repository[ResourceRecord]

    def execute(self: Self, request: DeleteUserRequest) -> None:
        """Delete the user.

        Args:
            request: The delete request.

        Raises:
            ValidationError: The user has policies attached, belongs to a
                group, has console access, or has access keys, and ``force``
                is ``False``. Nothing is deleted or detached in this case.
        """
        attached = self.gateway.list_attached_user_policies(request.name)
        groups = self.gateway.list_groups_for_user(request.name)
        login_profile = self.gateway.get_login_profile(request.name)
        access_keys = self.gateway.list_access_keys(request.name)

        if (attached or groups or login_profile or access_keys) and not request.force:
            parts = []
            if attached:
                names = ", ".join(policy.policy_name for policy in attached)
                parts.append(f"políticas adjuntas ({names})")
            if groups:
                names = ", ".join(group.group_name for group in groups)
                parts.append(f"grupos ({names})")
            if login_profile is not None:
                parts.append("acceso a consola")
            if access_keys:
                parts.append(f"{len(access_keys)} access key(s)")
            raise ValidationError(
                f"El usuario '{request.name}' tiene: {'; '.join(parts)}.",
                hint="Usa --force para limpiarlo todo y borrar el usuario de todas formas.",
            )

        for policy in attached:
            self.gateway.detach_user_policy(request.name, policy.policy_arn)
        for group in groups:
            self.gateway.remove_user_from_group(group.group_name, request.name)
        if login_profile is not None:
            self.gateway.delete_login_profile(request.name)
        for key in access_keys:
            self.gateway.delete_access_key(request.name, key.access_key_id)

        self.gateway.delete_user(request.name)
        self.repository.delete(f"iam:user:{request.name}")

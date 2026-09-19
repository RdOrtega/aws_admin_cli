"""Use case: delete a customer-managed IAM policy, refusing while still attached."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import DeletePolicyRequest
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository


@dataclass(frozen=True, slots=True)
class DeletePolicyUseCase:
    """Delete a customer-managed IAM policy: blocks while attachment_count > 0."""

    gateway: IamGateway
    repository: Repository[ResourceRecord]

    def execute(self: Self, request: DeletePolicyRequest) -> None:
        """Delete the policy.

        Args:
            request: The delete request.

        Raises:
            ValidationError: The policy is still attached to at least one
                entity and ``force`` is ``False``. Nothing is deleted in this
                case.
        """
        policy = self.gateway.get_policy(request.arn)

        if policy.attachment_count > 0 and not request.force:
            raise ValidationError(
                f"La política '{policy.policy_name}' está adjunta a "
                f"{policy.attachment_count} entidad(es).",
                hint="Usa --force para borrarla de todas formas.",
            )

        self.gateway.delete_policy(request.arn)
        self.repository.delete(f"iam:policy:{request.arn}")

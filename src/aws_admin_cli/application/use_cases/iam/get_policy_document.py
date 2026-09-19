"""Use case: fetch an IAM policy's document (for ``iam policy get --show-document``)."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.domain.models.iam import PolicyDocument
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class GetPolicyDocumentUseCase:
    """Fetch a policy's document. Kept as a use case so the CLI never calls the gateway directly."""

    gateway: IamGateway

    def execute(self: Self, arn: str, version_id: str | None = None) -> PolicyDocument:
        """Return the document for policy ``arn``, defaulting to its current default version."""
        return self.gateway.get_policy_document(arn, version_id)

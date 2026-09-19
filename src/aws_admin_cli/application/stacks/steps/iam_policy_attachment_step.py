"""Step: ``iam:policy-attachment`` -- adapts a manifest resource to Attach/List/DetachPolicyUseCase.

Properties: ``policy_arn``, ``principal_type`` (``user``/``role``),
``principal_name``. No outputs meant for other resources to reference --
just enough recorded (for the step's own ``compensate``) to detach later.
"""

from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Self, cast

from aws_admin_cli.application.dto.iam import AttachPolicyRequest, DetachPolicyRequest
from aws_admin_cli.application.stacks.steps._shared import require_str
from aws_admin_cli.application.stacks.steps.base import StepResult
from aws_admin_cli.application.use_cases.iam.attach_policy import AttachPolicyUseCase
from aws_admin_cli.application.use_cases.iam.detach_policy import DetachPolicyUseCase
from aws_admin_cli.application.use_cases.iam.list_attached_policies import (
    ListAttachedPoliciesUseCase,
)
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState

_PRINCIPAL_TYPES = ("user", "role")


def _validate_principal_type(value: str, resource_id: str) -> Literal["user", "role"]:
    if value not in _PRINCIPAL_TYPES:
        raise ValidationError(
            f"'{value}' no es un principal_type válido para '{resource_id}'.",
            hint="Usa 'user' o 'role'.",
        )
    return cast(Literal["user", "role"], value)


@dataclass(frozen=True, slots=True)
class IamPolicyAttachmentStep:
    """Adapts a manifest's ``iam:policy-attachment`` resource to Attach/List/DetachPolicyUseCase."""

    kind: ClassVar[ResourceKind] = ResourceKind.IAM_POLICY_ATTACHMENT

    list_use_case: ListAttachedPoliciesUseCase
    attach_use_case: AttachPolicyUseCase
    detach_use_case: DetachPolicyUseCase

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        """Attach the policy (idempotent: a no-op if already attached)."""
        policy_arn = require_str(props, "policy_arn", spec.id)
        principal_name = require_str(props, "principal_name", spec.id)
        principal_type = _validate_principal_type(
            require_str(props, "principal_type", spec.id), spec.id
        )

        already_attached = any(
            attached.policy_arn == policy_arn
            for attached in self.list_use_case.execute(principal_name, principal_type)
        )
        self.attach_use_case.execute(
            AttachPolicyRequest(
                principal_name=principal_name,
                policy_arn=policy_arn,
                principal_type=principal_type,
            )
        )
        return StepResult(
            physical_id=f"{principal_type}:{principal_name}:{policy_arn}",
            arn=None,
            outputs={
                "policy_arn": policy_arn,
                "principal_name": principal_name,
                "principal_type": principal_type,
            },
            created_by_stack=not already_attached,
        )

    def compensate(self: Self, state: ResourceState) -> None:
        """Detach the policy."""
        policy_arn = state.outputs.get("policy_arn")
        principal_name = state.outputs.get("principal_name")
        principal_type = state.outputs.get("principal_type")
        if policy_arn is None or principal_name is None or principal_type not in _PRINCIPAL_TYPES:
            return
        self.detach_use_case.execute(
            DetachPolicyRequest(
                principal_name=principal_name,
                policy_arn=policy_arn,
                principal_type=cast(Literal["user", "role"], principal_type),
            )
        )

    def still_exists(self: Self, state: ResourceState) -> bool:
        """Whether the attachment is still there (drift detection)."""
        policy_arn = state.outputs.get("policy_arn")
        principal_name = state.outputs.get("principal_name")
        principal_type = state.outputs.get("principal_type")
        if policy_arn is None or principal_name is None or principal_type not in _PRINCIPAL_TYPES:
            return False
        attached = self.list_use_case.execute(
            principal_name, cast(Literal["user", "role"], principal_type)
        )
        return any(item.policy_arn == policy_arn for item in attached)

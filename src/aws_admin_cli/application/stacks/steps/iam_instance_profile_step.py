"""Step: ``iam:instance-profile`` -- adapts a manifest resource to a Fase 5 bridge use case.

Delegates to ``ensure_instance_profile_for_role``, the IAM/EC2 bridge use case.

Properties: ``role_name``, optional ``profile_name`` (must equal
``role_name`` if given -- see ``IamInstanceProfileStep``'s docstring for
why). Outputs: ``arn``, ``name``.
"""

from dataclasses import dataclass
from typing import Any, ClassVar, Self

from aws_admin_cli.application.dto.iam import DeleteInstanceProfileRequest
from aws_admin_cli.application.stacks.steps._shared import optional_str, require_str
from aws_admin_cli.application.stacks.steps.base import StepResult
from aws_admin_cli.application.use_cases.iam.delete_instance_profile import (
    DeleteInstanceProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.ensure_instance_profile_for_role import (
    EnsureInstanceProfileForRoleUseCase,
)
from aws_admin_cli.application.use_cases.iam.get_instance_profile import GetInstanceProfileUseCase
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState


@dataclass(frozen=True, slots=True)
class IamInstanceProfileStep:
    """Adapts a manifest's ``iam:instance-profile`` resource to the ensure-profile use case.

    Deliberately delegates ALL of the create-and-attach logic to that
    existing (Fase 5) use case rather than composing
    Create/AttachRoleToProfileUseCase itself -- re-doing that composition
    here would be exactly the "steps reimplement a use case" mistake this
    module's docstring warns against. The one consequence worth naming:
    ``ensure_instance_profile_for_role`` uses the role's own name as the
    profile's name (a fixed 1:1 convention), so this step can't honor a
    ``profile_name`` that differs from ``role_name`` -- it raises instead of
    silently ignoring one or the other.
    """

    kind: ClassVar[ResourceKind] = ResourceKind.IAM_INSTANCE_PROFILE

    get_use_case: GetInstanceProfileUseCase
    ensure_use_case: EnsureInstanceProfileForRoleUseCase
    delete_use_case: DeleteInstanceProfileUseCase

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        """Ensure the instance profile exists and wraps ``role_name``."""
        role_name = require_str(props, "role_name", spec.id)
        profile_name = optional_str(props, "profile_name") or role_name
        if profile_name != role_name:
            raise ValidationError(
                f"El recurso '{spec.id}' (iam:instance-profile) especifica profile_name "
                f"('{profile_name}') distinto de role_name ('{role_name}').",
                hint="Este step delega en ensure_instance_profile_for_role, que usa el "
                "nombre del rol como nombre del instance profile (convención 1:1) -- omite "
                "'profile_name' o iguálalo a 'role_name'.",
            )

        existed_before = self._exists(role_name)
        arn = self.ensure_use_case.execute(role_name)
        return StepResult(
            physical_id=role_name,
            arn=arn,
            outputs={"arn": arn, "name": role_name},
            created_by_stack=not existed_before,
        )

    def _exists(self: Self, name: str) -> bool:
        try:
            self.get_use_case.execute(name)
            return True
        except ResourceNotFoundError:
            return False

    def compensate(self: Self, state: ResourceState) -> None:
        """Delete the instance profile (``--force``: detaches the role first)."""
        if state.physical_id is None:
            return
        self.delete_use_case.execute(
            DeleteInstanceProfileRequest(name=state.physical_id, force=True)
        )

    def still_exists(self: Self, state: ResourceState) -> bool:
        """Whether the instance profile is still there (drift detection)."""
        return state.physical_id is not None and self._exists(state.physical_id)

"""Step: ``iam:role`` -- adapts a manifest resource to Create/Get/DeleteRoleUseCase.

Properties: ``role_name``, one of ``service``/``trust_policy_file``,
optional ``description``/``max_session_duration``. Outputs: ``arn``, ``name``.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Self

from aws_admin_cli.application.dto.iam import (
    CreateRoleRequest,
    DeleteInstanceProfileRequest,
    DeleteRoleRequest,
)
from aws_admin_cli.application.stacks.steps._shared import (
    optional_int,
    optional_str,
    require_str,
)
from aws_admin_cli.application.stacks.steps.base import StepResult
from aws_admin_cli.application.use_cases.iam.create_role import CreateRoleUseCase
from aws_admin_cli.application.use_cases.iam.delete_instance_profile import (
    DeleteInstanceProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.delete_role import DeleteRoleUseCase
from aws_admin_cli.application.use_cases.iam.get_instance_profile import GetInstanceProfileUseCase
from aws_admin_cli.application.use_cases.iam.get_role import GetRoleUseCase
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.iam import PolicyDocument, service_trust_policy
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState


def _resolve_trust_policy(props: dict[str, Any], resource_id: str) -> PolicyDocument:
    service = props.get("service")
    trust_policy_file = props.get("trust_policy_file")
    if service is not None and trust_policy_file is not None:
        raise ValidationError(
            f"El recurso '{resource_id}' especifica 'service' y 'trust_policy_file' a la vez.",
            hint="Usa solo una de las dos properties.",
        )
    if trust_policy_file is not None:
        return PolicyDocument.from_aws(Path(str(trust_policy_file)).read_text(encoding="utf-8"))
    if service is not None:
        return service_trust_policy(str(service))
    raise ValidationError(
        f"El recurso '{resource_id}' (iam:role) necesita 'service' o 'trust_policy_file'.",
        hint="Añade `service: ec2.amazonaws.com` (u otro principal), o "
        "`trust_policy_file: ruta.json`.",
    )


@dataclass(frozen=True, slots=True)
class IamRoleStep:
    """Adapts a manifest's ``iam:role`` resource to Create/Get/DeleteRoleUseCase."""

    kind: ClassVar[ResourceKind] = ResourceKind.IAM_ROLE

    get_use_case: GetRoleUseCase
    create_use_case: CreateRoleUseCase
    delete_use_case: DeleteRoleUseCase
    get_instance_profile_use_case: GetInstanceProfileUseCase
    delete_instance_profile_use_case: DeleteInstanceProfileUseCase

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        """Create the role, or reuse it (``created_by_stack=False``) if it already exists."""
        role_name = require_str(props, "role_name", spec.id)
        trust_policy = _resolve_trust_policy(props, spec.id)

        existed_before = self._exists(role_name)
        role = self.create_use_case.execute(
            CreateRoleRequest(
                name=role_name,
                trust_policy=trust_policy,
                description=optional_str(props, "description"),
                max_session_duration=optional_int(props, "max_session_duration"),
                if_not_exists=True,
            )
        )
        return StepResult(
            physical_id=role.role_name,
            arn=role.arn,
            outputs={"arn": role.arn, "name": role.role_name},
            created_by_stack=not existed_before,
        )

    def _exists(self: Self, role_name: str) -> bool:
        try:
            self.get_use_case.execute(role_name)
            return True
        except ResourceNotFoundError:
            return False

    def compensate(self: Self, state: ResourceState) -> None:
        """Delete the role (``--force``: detaches any policy first).

        FIRST deletes a same-named instance profile, if one exists --
        ``ensure_instance_profile_for_role`` (the use case ``Ec2InstanceStep``
        delegates to for ``--iam-role``) creates one using the role's own
        name (its fixed 1:1 convention), as a side effect this stack never
        tracks as its own ``ResourceState``. Without this, ``DeleteRole``
        fails outright with ``DeleteConflict`` ("must remove roles from
        instance profile first") whenever an ``ec2:instance`` resource used
        this role via ``iam_role`` -- confirmed against real LocalStack, not
        a hypothetical.
        """
        if state.physical_id is None:
            return
        self._delete_instance_profile_if_present(state.physical_id)
        self.delete_use_case.execute(DeleteRoleRequest(name=state.physical_id, force=True))

    def _delete_instance_profile_if_present(self: Self, role_name: str) -> None:
        try:
            self.get_instance_profile_use_case.execute(role_name)
        except ResourceNotFoundError:
            return
        self.delete_instance_profile_use_case.execute(
            DeleteInstanceProfileRequest(name=role_name, force=True)
        )

    def still_exists(self: Self, state: ResourceState) -> bool:
        """Whether the role is still there (drift detection)."""
        return state.physical_id is not None and self._exists(state.physical_id)

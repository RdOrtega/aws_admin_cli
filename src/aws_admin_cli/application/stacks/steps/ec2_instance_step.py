"""Step: ``ec2:instance`` -- adapts a manifest resource to Launch/Get/TerminateInstanceUseCase.

Properties: ``name``, ``ami``, ``instance_type``, ``subnet``,
``security_groups`` (list), one of ``iam_role``/``iam_instance_profile``,
optional ``key_name``, ``user_data_file``, ``volume_size``, ``public_ip``,
``confirm_public``, ``confirm_large``, ``count``, ``tags``. Outputs:
``instance_id``, ``private_ip``, ``public_ip``, ``az``.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Self

from aws_admin_cli.application.dto.ec2 import LaunchInstanceRequest
from aws_admin_cli.application.stacks.steps._shared import (
    optional_bool,
    optional_int,
    optional_str,
    require,
    require_str,
)
from aws_admin_cli.application.stacks.steps.base import StepResult
from aws_admin_cli.application.use_cases.ec2.get_instance import GetInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.launch_instance import LaunchInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.terminate_instance import TerminateInstanceUseCase
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.ec2 import InstanceState
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, ResourceState

_DEFAULT_TERMINATE_TIMEOUT_S = 300


def _security_group_refs(props: dict[str, Any], resource_id: str) -> tuple[str, ...]:
    raw = require(props, "security_groups", resource_id)
    if not isinstance(raw, list) or not raw:
        raise ValidationError(
            f"El recurso '{resource_id}' (ec2:instance): 'security_groups' debe ser una "
            "lista no vacía.",
            hint="Ejemplo: `security_groups: [corp-web-sg]`.",
        )
    return tuple(str(item) for item in raw)


def _tags(props: dict[str, Any]) -> dict[str, str]:
    raw = props.get("tags") or {}
    if not isinstance(raw, dict):
        raise ValidationError(
            "'tags' debe ser un mapa clave/valor.", hint="Ejemplo: `tags: {Owner: ruben}`."
        )
    return {str(key): str(value) for key, value in raw.items()}


def _iam_selection(props: dict[str, Any], resource_id: str) -> tuple[str | None, str | None]:
    iam_role = optional_str(props, "iam_role")
    iam_instance_profile = optional_str(props, "iam_instance_profile")
    if iam_role and iam_instance_profile:
        raise ValidationError(
            f"El recurso '{resource_id}' (ec2:instance) especifica 'iam_role' e "
            "'iam_instance_profile' a la vez.",
            hint="Usa 'iam_role' (nombre de rol; se garantiza/crea su instance profile) o "
            "'iam_instance_profile' (ARN de un instance profile ya existente) -- no ambos.",
        )
    return iam_role, iam_instance_profile


@dataclass(frozen=True, slots=True)
class Ec2InstanceStep:
    """Adapts a manifest's ``ec2:instance`` resource to Launch/Get/TerminateInstanceUseCase.

    ``execute`` always waits for RUNNING (``wait=True``) -- a stack apply is
    a synchronous, ordered sequence; a later step referencing
    ``${this.private_ip}`` needs that output to already be real, not a
    placeholder for an instance still PENDING.
    """

    kind: ClassVar[ResourceKind] = ResourceKind.EC2_INSTANCE

    launch_use_case: LaunchInstanceUseCase
    get_use_case: GetInstanceUseCase
    terminate_use_case: TerminateInstanceUseCase
    timeout_s: int = _DEFAULT_TERMINATE_TIMEOUT_S

    def execute(self: Self, spec: ResourceSpec, props: dict[str, Any]) -> StepResult:
        """Launch the instance and wait for it to reach RUNNING."""
        iam_role, iam_instance_profile_arn = _iam_selection(props, spec.id)
        user_data_file = optional_str(props, "user_data_file")

        instance = self.launch_use_case.execute(
            LaunchInstanceRequest(
                name=require_str(props, "name", spec.id),
                ami_ref=require_str(props, "ami", spec.id),
                instance_type=require_str(props, "instance_type", spec.id),
                subnet_ref=require_str(props, "subnet", spec.id),
                security_group_refs=_security_group_refs(props, spec.id),
                key_name=optional_str(props, "key_name"),
                iam_role=iam_role,
                iam_instance_profile_arn=iam_instance_profile_arn,
                user_data=(
                    Path(user_data_file).read_text(encoding="utf-8") if user_data_file else None
                ),
                volume_size_gb=optional_int(props, "volume_size") or 8,
                assign_public_ip=optional_bool(props, "public_ip"),
                confirm_public=optional_bool(props, "confirm_public"),
                confirm_large=optional_bool(props, "confirm_large"),
                count=optional_int(props, "count") or 1,
                tags=_tags(props),
                wait=True,
                timeout_s=self.timeout_s,
            )
        )
        outputs = {"instance_id": instance.instance_id}
        if instance.private_ip_address:
            outputs["private_ip"] = instance.private_ip_address
        if instance.public_ip_address:
            outputs["public_ip"] = instance.public_ip_address
        if instance.availability_zone:
            outputs["az"] = instance.availability_zone
        return StepResult(
            physical_id=instance.instance_id, arn=None, outputs=outputs, created_by_stack=True
        )

    def compensate(self: Self, state: ResourceState) -> None:
        """Terminate the instance and WAIT for TERMINATED before returning.

        Without waiting here, a subsequent compensation of this instance's
        ``iam:instance-profile`` (or ``iam:role``) resource -- reverse
        topological order compensates the instance FIRST, the profile/role
        after -- would run against a still-running (or still-terminating)
        instance, and IAM refuses to detach/delete a role still attached to
        an instance profile currently in use. This wait is the entire reason
        rollback compensates sequentially rather than firing every
        compensation in parallel.
        """
        if state.physical_id is None:
            return
        instance = self.get_use_case.execute(state.physical_id)
        if not instance.state.can_terminate:
            return
        self.terminate_use_case.execute(
            instance, force=False, dry_run=False, wait=True, timeout_s=self.timeout_s
        )

    def still_exists(self: Self, state: ResourceState) -> bool:
        """Whether the instance is still there and not TERMINATED (drift detection)."""
        if state.physical_id is None:
            return False
        try:
            instance = self.get_use_case.execute(state.physical_id)
        except ResourceNotFoundError:
            return False
        return instance.state is not InstanceState.TERMINATED

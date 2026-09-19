"""Use case: guarantee a role has a usable instance profile, idempotently.

The composed use case EC2 (Fase 5) actually calls: given a role name, make
sure an instance profile exists with that role attached, creating whatever is
missing, and hand back the profile's ARN -- that's what
``ec2 instance launch --iam-role ROLE`` passes to ``run_instance`` as
``IamInstanceProfile={"Arn": ...}``.
"""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import (
    AttachRoleToProfileRequest,
    CreateInstanceProfileRequest,
)
from aws_admin_cli.application.use_cases.iam.attach_role_to_profile import (
    AttachRoleToProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.create_instance_profile import (
    CreateInstanceProfileUseCase,
)
from aws_admin_cli.core.exceptions import ResourceNotFoundError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository


@dataclass(frozen=True, slots=True)
class EnsureInstanceProfileForRoleUseCase:
    """Ensure ``role_name`` has an instance profile wrapping it, creating what's missing.

    Uses the role's own name as the instance profile's name -- a single,
    predictable 1:1 naming convention, so calling this twice for the same
    role resolves to the same profile rather than accumulating one profile
    per call. Composes ``CreateInstanceProfileUseCase`` (with
    ``if_not_exists=True``) and ``AttachRoleToProfileUseCase`` (already a
    no-op if the role is attached), so calling this use case twice for the
    same role does zero mutating AWS calls the second time.
    """

    gateway: IamGateway
    repository: Repository[ResourceRecord]
    profile: str
    region: str

    def execute(self: Self, role_name: str) -> str:
        """Ensure an instance profile wraps ``role_name`` and return its ARN.

        Raises:
            ResourceNotFoundError: ``role_name`` doesn't exist -- this use
                case never creates the role itself, only the profile around it.
        """
        try:
            self.gateway.get_role(role_name)
        except ResourceNotFoundError as exc:
            raise ResourceNotFoundError(
                f"El rol '{role_name}' no existe.",
                hint="Créalo primero con `iam role create`.",
                aws_code=exc.aws_code,
            ) from exc

        create_use_case = CreateInstanceProfileUseCase(
            gateway=self.gateway,
            repository=self.repository,
            profile=self.profile,
            region=self.region,
        )
        create_use_case.execute(CreateInstanceProfileRequest(name=role_name, if_not_exists=True))

        attach_use_case = AttachRoleToProfileUseCase(gateway=self.gateway)
        instance_profile = attach_use_case.execute(
            AttachRoleToProfileRequest(profile_name=role_name, role_name=role_name)
        )
        return instance_profile.arn

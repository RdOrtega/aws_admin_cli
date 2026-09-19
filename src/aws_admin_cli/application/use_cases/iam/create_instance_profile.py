"""Use case: create an IAM instance profile, idempotently."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from aws_admin_cli.application.dto.iam import CreateInstanceProfileRequest
from aws_admin_cli.core.exceptions import ResourceNotFoundError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.iam import IamInstanceProfile
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository


@dataclass(frozen=True, slots=True)
class CreateInstanceProfileUseCase:
    """Create an instance profile: idempotent, tracked in the local ledger."""

    gateway: IamGateway
    repository: Repository[ResourceRecord]
    profile: str
    region: str

    def execute(self: Self, request: CreateInstanceProfileRequest) -> IamInstanceProfile:
        """Create the instance profile, or return the existing one if ``if_not_exists``.

        Args:
            request: The create request.

        Returns:
            The created ``IamInstanceProfile`` -- or, when ``if_not_exists`` is
            set and a profile by that name already exists, that pre-existing one.
        """
        if request.if_not_exists:
            existing = self._find_existing(request.name)
            if existing is not None:
                return existing

        instance_profile = self.gateway.create_instance_profile(request.name, request.path)
        self._track(instance_profile)
        return instance_profile

    def _find_existing(self: Self, name: str) -> IamInstanceProfile | None:
        try:
            return self.gateway.get_instance_profile(name)
        except ResourceNotFoundError:
            return None

    def _track(self: Self, instance_profile: IamInstanceProfile) -> None:
        self.repository.save(
            ResourceRecord(
                resource_type="iam:instance-profile",
                identifier=instance_profile.instance_profile_name,
                arn=instance_profile.arn,
                profile=self.profile,
                region=self.region,
                created_at=datetime.now(UTC),
                metadata={"path": instance_profile.path},
            )
        )

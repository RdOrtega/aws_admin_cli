"""Use case: create an IAM group, idempotently."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import CreateGroupRequest
from aws_admin_cli.domain.models.iam import IamGroup
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class CreateGroupUseCase:
    """Create an IAM group: idempotent when ``if_not_exists`` is set."""

    gateway: IamGateway

    def execute(self: Self, request: CreateGroupRequest) -> IamGroup:
        """Create the group, or return the existing one if ``if_not_exists`` and it exists."""
        if request.if_not_exists:
            existing = self._find_existing(request.name)
            if existing is not None:
                return existing
        return self.gateway.create_group(request.name, request.path)

    def _find_existing(self: Self, name: str) -> IamGroup | None:
        try:
            return next(g for g in self.gateway.list_groups(None) if g.group_name == name)
        except StopIteration:
            return None

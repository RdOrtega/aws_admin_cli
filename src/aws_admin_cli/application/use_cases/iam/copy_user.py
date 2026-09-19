"""Use case: create a new IAM user by cloning an existing one's groups, policies, and tags."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.iam import CopyUserRequest, CreateUserRequest
from aws_admin_cli.application.use_cases.iam.create_user import CreateUserUseCase
from aws_admin_cli.domain.models.iam import IamUser
from aws_admin_cli.domain.ports.iam_gateway import IamGateway


@dataclass(frozen=True, slots=True)
class CopyUserUseCase:
    """Clone a user's tags, group memberships, and attached policies onto a brand-new one.

    Delegates the actual creation to ``CreateUserUseCase`` (auto-tagging,
    ledger-tracking, and all) rather than reimplementing it -- copying a user
    is "create a user, then also do these extra attachment steps", not a
    parallel creation path.
    """

    gateway: IamGateway
    create_user: CreateUserUseCase

    def execute(self: Self, request: CopyUserRequest) -> IamUser:
        """Create ``request.new_name`` with ``request.source_name``'s tags, groups, and policies.

        Raises:
            ResourceNotFoundError: ``request.source_name`` doesn't exist.
            ResourceAlreadyExistsError: ``request.new_name`` already exists.
        """
        source = self.gateway.get_user(request.source_name)
        source_tags = {tag["Key"]: tag["Value"] for tag in source.tags}

        new_user = self.create_user.execute(
            CreateUserRequest(name=request.new_name, path=request.path, tags=source_tags)
        )

        for group in self.gateway.list_groups_for_user(request.source_name):
            self.gateway.add_user_to_group(group.group_name, request.new_name)

        for policy in self.gateway.list_attached_user_policies(request.source_name):
            self.gateway.attach_user_policy(request.new_name, policy.policy_arn)

        return new_user

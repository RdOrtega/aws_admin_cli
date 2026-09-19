"""Use case: list instances across every AWS region, for the EC2 List/Filter screen."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.ec2 import ListInstancesRequest
from aws_admin_cli.application.services.network_resolver import NetworkResolver
from aws_admin_cli.application.use_cases.ec2.list_instances import build_instance_filters
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway


@dataclass(frozen=True, slots=True)
class ListInstancesGlobalUseCase:
    """Same filters as ``ListInstancesUseCase``, scanned across every region at once.

    The active session region (``ctx.settings.region``) never enters into
    this -- it stays the target for new-instance creation only. Each
    returned ``Instance.region`` records where it was actually found.
    """

    gateway: Ec2Gateway
    network_resolver: NetworkResolver

    def execute(self: Self, request: ListInstancesRequest) -> list[Instance]:
        """Return instances matching every filter set on ``request``, from every region."""
        filters = build_instance_filters(request, self.network_resolver)
        instances = self.gateway.describe_instances_all_regions(filters or None)
        if request.managed_only:
            instances = [instance for instance in instances if instance.managed_by_cli]
        return instances

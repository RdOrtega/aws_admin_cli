"""Use case: list instances, with state/tag/VPC/subnet/managed-only filters."""

from dataclasses import dataclass
from typing import Self

from aws_admin_cli.application.dto.ec2 import ListInstancesRequest
from aws_admin_cli.application.services.network_resolver import NetworkResolver
from aws_admin_cli.domain.models.ec2 import Instance
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway

# Terminated (and terminating) instances are hidden by default, everywhere --
# not just in the TUI. AWS itself keeps a terminated instance visible in
# DescribeInstances for a while (by design), which would otherwise leave
# "ghost" rows in every list/search view long after a user terminated
# something. A caller that explicitly wants one particular state (including
# "terminated", e.g. to audit what was recently torn down) still gets exactly
# that single state via ``request.state`` -- this default only applies when
# no explicit state filter was requested.
_DEFAULT_VISIBLE_STATES: tuple[str, ...] = ("running", "stopped", "pending", "stopping")


def build_instance_filters(
    request: ListInstancesRequest, network_resolver: NetworkResolver
) -> dict[str, list[str]]:
    """Translate ``request``'s filters into ``DescribeInstances`` ``Filters`` form.

    Shared by ``ListInstancesUseCase`` (single, active region) and
    ``ListInstancesGlobalUseCase`` (every region) so the two never drift
    apart on what a given ``ListInstancesRequest`` actually means.
    """
    filters: dict[str, list[str]] = {}
    if request.state:
        filters["instance-state-name"] = [request.state]
    else:
        filters["instance-state-name"] = list(_DEFAULT_VISIBLE_STATES)
    if request.tag_name:
        filters["tag:Name"] = [request.tag_name]
    if request.vpc_ref:
        vpc = network_resolver.resolve_vpc(request.vpc_ref)
        filters["vpc-id"] = [vpc.vpc_id]
    if request.subnet_ref:
        subnet = network_resolver.resolve_subnet(request.subnet_ref)
        filters["subnet-id"] = [subnet.subnet_id]
    return filters


@dataclass(frozen=True, slots=True)
class ListInstancesUseCase:
    """List instances matching ``request``'s filters.

    Hides terminated/terminating instances by default (see
    ``_DEFAULT_VISIBLE_STATES``) unless ``request.state`` asks for a specific
    state explicitly.
    """

    gateway: Ec2Gateway
    network_resolver: NetworkResolver

    def execute(self: Self, request: ListInstancesRequest) -> list[Instance]:
        """Return instances matching every filter set on ``request``."""
        filters = build_instance_filters(request, self.network_resolver)
        instances = self.gateway.describe_instances(None, filters or None)
        if request.managed_only:
            instances = [instance for instance in instances if instance.managed_by_cli]
        return instances

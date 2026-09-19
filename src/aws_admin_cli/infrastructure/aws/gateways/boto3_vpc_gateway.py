"""The boto3-backed ``VpcGateway`` implementation -- SOLO LECTURA.

VPC/subnet/security-group/route-table operations live on the ``ec2`` boto3
client (there is no separate "vpc" service client), so this gateway uses
``client_factory.ec2()``. Every method is wrapped in :func:`aws_error_boundary`
and every listing operation is fully paginated, same conventions as
``boto3_iam_gateway.py`` and ``boto3_s3_gateway.py``.

This module contains ZERO mutating calls -- not even private ones. See
``domain/ports/vpc_gateway.py``'s module docstring for why, and
``tests/unit/architecture/test_vpc_readonly.py`` for the automated check that
enforces it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, cast

from aws_admin_cli.core.exceptions import AwsError
from aws_admin_cli.domain.models.vpc import (
    AvailabilityZone,
    RouteTable,
    SecurityGroup,
    Subnet,
    Vpc,
)
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.error_mapper import aws_error_boundary

if TYPE_CHECKING:
    from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway


def _to_filters(mapping: Mapping[str, Sequence[str]] | None) -> list[dict[str, Any]]:
    """Convert ``{"vpc-id": ["vpc-123"]}`` into EC2's ``[{"Name": ..., "Values": [...]}]``."""
    if not mapping:
        return []
    return [{"Name": name, "Values": list(values)} for name, values in mapping.items()]


# EC2 Filter ``Name`` -> ``SecurityGroup`` field, for the in-Python filtering
# ``describe_security_groups`` uses instead of a real ``Filters`` kwarg (see its
# docstring). Only the names any caller could plausibly pass for security groups.
_SECURITY_GROUP_FILTER_FIELDS: dict[str, str] = {
    "vpc-id": "vpc_id",
    "group-name": "group_name",
    "group-id": "group_id",
}


@dataclass(frozen=True, slots=True)
class Boto3VpcGateway:
    """``VpcGateway`` implemented against a real (or LocalStack) EC2 client. Read-only."""

    client_factory: ClientFactory

    def _client(self: Self, *, region: str | None = None) -> Any:
        return self.client_factory.ec2(region=region)

    def describe_vpcs(
        self: Self,
        vpc_ids: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Vpc]:
        """List VPCs, fully paginated."""
        kwargs: dict[str, Any] = {}
        if vpc_ids:
            kwargs["VpcIds"] = list(vpc_ids)
        if filters:
            kwargs["Filters"] = _to_filters(filters)

        vpcs: list[Vpc] = []
        with aws_error_boundary("ec2", "DescribeVpcs"):
            paginator = self._client(region=region).get_paginator("describe_vpcs")
            for page in paginator.paginate(**kwargs):
                vpcs.extend(Vpc.model_validate(raw) for raw in page.get("Vpcs", []))
        return vpcs

    def describe_subnets(
        self: Self,
        subnet_ids: Sequence[str] | None,
        vpc_id: str | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Subnet]:
        """List subnets, fully paginated, with ``is_public`` resolved via the VPC's route tables.

        AWS never tells you directly whether a subnet is public: it has to be
        derived by finding the route table associated with the subnet (or,
        for a subnet with no explicit association, the VPC's MAIN route
        table -- the fallback most implementations forget) and checking
        whether that table routes ``0.0.0.0/0`` to an internet gateway. When
        the owning VPC can't be determined for a subnet (e.g. filtered
        results spanning multiple VPCs with permission gaps), ``is_public``
        is left ``None`` rather than guessed as ``False``.
        """
        kwargs: dict[str, Any] = {}
        if subnet_ids:
            kwargs["SubnetIds"] = list(subnet_ids)
        merged_filters = dict(filters or {})
        if vpc_id:
            merged_filters["vpc-id"] = [vpc_id]
        if merged_filters:
            kwargs["Filters"] = _to_filters(merged_filters)

        subnets: list[Subnet] = []
        with aws_error_boundary("ec2", "DescribeSubnets"):
            paginator = self._client(region=region).get_paginator("describe_subnets")
            for page in paginator.paginate(**kwargs):
                subnets.extend(Subnet.model_validate(raw) for raw in page.get("Subnets", []))

        return [self._with_is_public(subnet, region=region) for subnet in subnets]

    def _with_is_public(self: Self, subnet: Subnet, *, region: str | None = None) -> Subnet:
        try:
            route_tables = self.describe_route_tables(subnet.vpc_id, region=region)
        except AwsError:
            # Can't be determined (e.g. no permission to describe route tables) --
            # leave is_public as None rather than guessing False.
            return subnet.model_copy(update={"is_public": None})

        associated = next(
            (rt for rt in route_tables if subnet.subnet_id in rt.associated_subnet_ids),
            None,
        )
        if associated is None:
            # No explicit association: falls back to the VPC's main route table.
            associated = next((rt for rt in route_tables if rt.is_main), None)
        if associated is None:
            return subnet.model_copy(update={"is_public": None})
        return subnet.model_copy(update={"is_public": associated.has_igw_route})

    def describe_security_groups(
        self: Self,
        group_ids: Sequence[str] | None,
        vpc_id: str | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[SecurityGroup]:
        """List security groups, fully paginated.

        Never passes ``Filters`` to ``DescribeSecurityGroups``: this LocalStack
        raises an InternalError ('vpc-id') the instant any Filter is attached to
        that specific operation (``GroupIds`` is unaffected -- see
        ``infrastructure/local/sg_seed.py`` for the same bug hit from the write
        side). ``vpc_id`` and ``filters`` are therefore applied in Python instead,
        after fetching the full (paginated, unfiltered) list.
        """
        kwargs: dict[str, Any] = {}
        if group_ids:
            kwargs["GroupIds"] = list(group_ids)

        groups: list[SecurityGroup] = []
        with aws_error_boundary("ec2", "DescribeSecurityGroups"):
            paginator = self._client(region=region).get_paginator("describe_security_groups")
            for page in paginator.paginate(**kwargs):
                groups.extend(
                    SecurityGroup.model_validate(raw) for raw in page.get("SecurityGroups", [])
                )

        merged_filters = dict(filters or {})
        if vpc_id:
            merged_filters["vpc-id"] = [vpc_id]
        for name, values in merged_filters.items():
            field = _SECURITY_GROUP_FILTER_FIELDS.get(name)
            if field is None:
                continue
            allowed = set(values)
            groups = [group for group in groups if getattr(group, field) in allowed]
        return groups

    def describe_route_tables(
        self: Self, vpc_id: str | None, *, region: str | None = None
    ) -> list[RouteTable]:
        """List route tables for a VPC (or every route table, if ``vpc_id`` is ``None``)."""
        kwargs: dict[str, Any] = {}
        if vpc_id:
            kwargs["Filters"] = _to_filters({"vpc-id": [vpc_id]})

        tables: list[RouteTable] = []
        with aws_error_boundary("ec2", "DescribeRouteTables"):
            paginator = self._client(region=region).get_paginator("describe_route_tables")
            for page in paginator.paginate(**kwargs):
                tables.extend(RouteTable.model_validate(raw) for raw in page.get("RouteTables", []))
        return tables

    def describe_availability_zones(self: Self) -> list[AvailabilityZone]:
        """List availability zones in the configured region (not paginated by AWS)."""
        with aws_error_boundary("ec2", "DescribeAvailabilityZones"):
            response = self._client().describe_availability_zones()
        return [
            AvailabilityZone.model_validate(raw) for raw in response.get("AvailabilityZones", [])
        ]


if TYPE_CHECKING:
    # Static conformance check: mypy fails right here if Boto3VpcGateway's method
    # signatures ever drift from the VpcGateway Protocol.
    _vpc_gateway_conformance: VpcGateway = cast(Boto3VpcGateway, None)

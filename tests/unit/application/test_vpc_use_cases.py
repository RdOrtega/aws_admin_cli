"""Tests for the simple VPC use cases (list/get), using FakeVpcGateway (no Mock())."""

import logging

import pytest
from aws_admin_cli.application.dto.vpc import ListSubnetsRequest
from aws_admin_cli.application.use_cases.vpc.get_security_group import GetSecurityGroupUseCase
from aws_admin_cli.application.use_cases.vpc.get_subnet import GetSubnetUseCase
from aws_admin_cli.application.use_cases.vpc.get_vpc_details import GetVpcDetailsUseCase
from aws_admin_cli.application.use_cases.vpc.list_availability_zones import (
    ListAvailabilityZonesUseCase,
)
from aws_admin_cli.application.use_cases.vpc.list_security_groups import (
    ListSecurityGroupsUseCase,
)
from aws_admin_cli.application.use_cases.vpc.list_subnets import ListSubnetsUseCase
from aws_admin_cli.application.use_cases.vpc.list_vpcs import ListVpcsUseCase
from aws_admin_cli.core.exceptions import ResourceNotFoundError
from aws_admin_cli.domain.models.vpc import Subnet

from tests.fakes.vpc import (
    SG_BASTION_ID,
    SG_WEB_ID,
    SUBNET_PRIVATE_1A_ID,
    SUBNET_PUBLIC_1A_ID,
    VPC_ID,
    FakeVpcGateway,
)

_LOGGER = logging.getLogger("aws_admin_cli")


def test_list_vpcs_returns_every_vpc() -> None:
    vpcs = ListVpcsUseCase(gateway=FakeVpcGateway()).execute()
    assert [v.vpc_id for v in vpcs] == [VPC_ID]


def test_get_vpc_details_aggregates_subnets_and_security_groups() -> None:
    gateway = FakeVpcGateway()
    vpc = gateway.vpcs[0]

    details = GetVpcDetailsUseCase(gateway=gateway).execute(vpc)

    assert details.vpc.vpc_id == VPC_ID
    assert details.subnet_count == 4
    assert details.security_group_count == 5
    assert len(details.subnets) == details.subnet_count
    assert len(details.security_groups) == details.security_group_count


def test_list_subnets_filters_by_availability_zone() -> None:
    use_case = ListSubnetsUseCase(gateway=FakeVpcGateway(), logger=_LOGGER)
    subnets = use_case.execute(ListSubnetsRequest(availability_zone="us-east-1b"))
    assert {s.availability_zone for s in subnets} == {"us-east-1b"}


def test_list_subnets_public_only_returns_only_public_subnets() -> None:
    use_case = ListSubnetsUseCase(gateway=FakeVpcGateway(), logger=_LOGGER)
    subnets = use_case.execute(ListSubnetsRequest(public_only=True))
    assert {s.subnet_id for s in subnets} == {
        s.subnet_id for s in FakeVpcGateway().subnets if s.is_public
    }
    assert all(s.is_public for s in subnets)


def test_list_subnets_private_only_returns_only_private_subnets() -> None:
    use_case = ListSubnetsUseCase(gateway=FakeVpcGateway(), logger=_LOGGER)
    subnets = use_case.execute(ListSubnetsRequest(private_only=True))
    assert all(s.is_public is False for s in subnets)


def test_list_subnets_skips_subnets_with_unknown_visibility_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unknown = Subnet(
        subnet_id="subnet-unknown0",
        vpc_id=VPC_ID,
        cidr_block="10.0.99.0/24",
        availability_zone="us-east-1c",
        available_ip_address_count=251,
        state="available",
        is_public=None,
    )
    use_case = ListSubnetsUseCase(gateway=FakeVpcGateway(subnets=[unknown]), logger=_LOGGER)

    with caplog.at_level(logging.WARNING, logger="aws_admin_cli"):
        subnets = use_case.execute(ListSubnetsRequest(public_only=True))

    assert subnets == []
    assert any("subnet-unknown0" in record.getMessage() for record in caplog.records)


def test_get_subnet_by_exact_id() -> None:
    subnet = GetSubnetUseCase(gateway=FakeVpcGateway()).execute(SUBNET_PUBLIC_1A_ID)
    assert subnet.subnet_id == SUBNET_PUBLIC_1A_ID


def test_get_subnet_not_found_raises_with_hint() -> None:
    with pytest.raises(ResourceNotFoundError) as exc_info:
        GetSubnetUseCase(gateway=FakeVpcGateway()).execute("subnet-nope0000")
    assert "vpc subnet list" in exc_info.value.hint


def test_list_security_groups_scoped_to_a_vpc() -> None:
    groups = ListSecurityGroupsUseCase(gateway=FakeVpcGateway()).execute(VPC_ID)
    ids = {g.group_id for g in groups}
    assert len(groups) == 5
    assert {SG_WEB_ID, SG_BASTION_ID}.issubset(ids)
    assert all(g.vpc_id == VPC_ID for g in groups)


def test_get_security_group_by_exact_id() -> None:
    sg = GetSecurityGroupUseCase(gateway=FakeVpcGateway()).execute(SG_BASTION_ID)
    assert sg.group_id == SG_BASTION_ID


def test_get_security_group_not_found_raises_with_hint() -> None:
    with pytest.raises(ResourceNotFoundError) as exc_info:
        GetSecurityGroupUseCase(gateway=FakeVpcGateway()).execute("sg-nope0000")
    assert "vpc sg list" in exc_info.value.hint


def test_list_availability_zones_returns_every_zone() -> None:
    zones = ListAvailabilityZonesUseCase(gateway=FakeVpcGateway()).execute()
    assert {z.zone_name for z in zones} == {"us-east-1a", "us-east-1b"}


def test_resolve_subnet_helper_constant_matches_fake_data() -> None:
    # Sanity check that the shared fake's SUBNET_PRIVATE_1A_ID constant actually
    # points at a private subnet -- guards against the fixtures and the constants
    # drifting apart silently.
    subnet = GetSubnetUseCase(gateway=FakeVpcGateway()).execute(SUBNET_PRIVATE_1A_ID)
    assert subnet.is_public is False

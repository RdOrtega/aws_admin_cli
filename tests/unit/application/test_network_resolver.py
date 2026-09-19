"""Tests for NetworkResolver: reference resolution, ambiguity/not-found errors, and caching."""

import pytest
from aws_admin_cli.application.services.network_resolver import NetworkResolver
from aws_admin_cli.core.exceptions import ResourceNotFoundError, ValidationError
from aws_admin_cli.domain.models.vpc import SecurityGroup, Subnet, Vpc

from tests.fakes.vpc import (
    SG_BASTION_ID,
    SG_WEB_ID,
    SUBNET_PRIVATE_1A_ID,
    VPC_ID,
    FakeVpcGateway,
)

# -- resolve_vpc ---------------------------------------------------------------


def test_resolve_vpc_by_id() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    vpc = resolver.resolve_vpc(VPC_ID)
    assert vpc.vpc_id == VPC_ID


def test_resolve_vpc_by_name_tag() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    vpc = resolver.resolve_vpc("corp-main-vpc")
    assert vpc.vpc_id == VPC_ID


def test_resolve_vpc_none_falls_back_to_the_single_vpc() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    vpc = resolver.resolve_vpc(None)
    assert vpc.vpc_id == VPC_ID


def test_resolve_vpc_none_uses_the_default_vpc_when_marked() -> None:
    default_vpc = Vpc(
        vpc_id="vpc-default0", cidr_block="172.31.0.0/16", state="available", is_default=True
    )
    other_vpc = Vpc(
        vpc_id="vpc-other000", cidr_block="10.1.0.0/16", state="available", is_default=False
    )
    resolver = NetworkResolver(gateway=FakeVpcGateway(vpcs=[default_vpc, other_vpc]))
    resolved = resolver.resolve_vpc(None)
    assert resolved.vpc_id == "vpc-default0"


def test_resolve_vpc_none_with_several_vpcs_and_no_default_raises_validation_error() -> None:
    vpc_a = Vpc(vpc_id="vpc-aaaaaaaa", cidr_block="10.1.0.0/16", state="available")
    vpc_b = Vpc(vpc_id="vpc-bbbbbbbb", cidr_block="10.2.0.0/16", state="available")
    resolver = NetworkResolver(gateway=FakeVpcGateway(vpcs=[vpc_a, vpc_b]))

    with pytest.raises(ValidationError) as exc_info:
        resolver.resolve_vpc(None)

    assert "--vpc" in exc_info.value.hint
    assert "vpc-aaaaaaaa" in exc_info.value.hint
    assert "vpc-bbbbbbbb" in exc_info.value.hint


def test_resolve_vpc_not_found_lists_candidates_in_the_hint() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    with pytest.raises(ResourceNotFoundError) as exc_info:
        resolver.resolve_vpc("no-existe-esta-vpc")
    assert VPC_ID in exc_info.value.hint


# -- resolve_subnet --------------------------------------------------------------


def test_resolve_subnet_by_id() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    subnet = resolver.resolve_subnet(SUBNET_PRIVATE_1A_ID)
    assert subnet.subnet_id == SUBNET_PRIVATE_1A_ID


def test_resolve_subnet_by_name_tag() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    subnet = resolver.resolve_subnet("corp-private-1a")
    assert subnet.subnet_id == SUBNET_PRIVATE_1A_ID


def test_resolve_subnet_ambiguous_name_lists_both_ids() -> None:
    dup_a = Subnet(
        subnet_id="subnet-aaaaaaaa",
        vpc_id=VPC_ID,
        cidr_block="10.0.20.0/24",
        availability_zone="us-east-1a",
        available_ip_address_count=251,
        state="available",
        tags=[{"Key": "Name", "Value": "corp-shared-name"}],
    )
    dup_b = Subnet(
        subnet_id="subnet-bbbbbbbb",
        vpc_id=VPC_ID,
        cidr_block="10.0.21.0/24",
        availability_zone="us-east-1b",
        available_ip_address_count=251,
        state="available",
        tags=[{"Key": "Name", "Value": "corp-shared-name"}],
    )
    resolver = NetworkResolver(gateway=FakeVpcGateway(subnets=[dup_a, dup_b]))

    with pytest.raises(ValidationError) as exc_info:
        resolver.resolve_subnet("corp-shared-name")

    assert "subnet-aaaaaaaa" in exc_info.value.hint
    assert "subnet-bbbbbbbb" in exc_info.value.hint


def test_resolve_subnet_not_found_hint_names_a_real_available_subnet() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    with pytest.raises(ResourceNotFoundError) as exc_info:
        resolver.resolve_subnet("no-existe-esta-subnet")
    assert "corp-public-1a" in exc_info.value.hint or SUBNET_PRIVATE_1A_ID in exc_info.value.hint


# -- resolve_security_group -------------------------------------------------------


def test_resolve_security_group_by_id() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    sg = resolver.resolve_security_group(SG_WEB_ID)
    assert sg.group_id == SG_WEB_ID


def test_resolve_security_group_by_group_name() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    sg = resolver.resolve_security_group("corp-bastion-sg")
    assert sg.group_id == SG_BASTION_ID


def test_resolve_security_group_by_name_tag_when_group_name_does_not_match() -> None:
    tagged = SecurityGroup.model_validate(
        {
            "GroupId": "sg-tagged00",
            "GroupName": "sg-1234567",
            "VpcId": VPC_ID,
            "Description": "tagged",
            "Tags": [{"Key": "Name", "Value": "corp-tagged-sg"}],
        }
    )
    resolver = NetworkResolver(gateway=FakeVpcGateway(security_groups=[tagged]))
    resolved = resolver.resolve_security_group("corp-tagged-sg")
    assert resolved.group_id == "sg-tagged00"


def test_resolve_security_group_not_found_raises() -> None:
    resolver = NetworkResolver(gateway=FakeVpcGateway())
    with pytest.raises(ResourceNotFoundError):
        resolver.resolve_security_group("no-existe-este-sg")


# -- Caching ----------------------------------------------------------------------


def test_resolving_three_subnets_costs_exactly_one_describe_subnets_call() -> None:
    gateway = FakeVpcGateway()
    resolver = NetworkResolver(gateway=gateway)

    resolver.resolve_subnet("corp-public-1a")
    resolver.resolve_subnet("corp-public-1b")
    resolver.resolve_subnet("corp-private-1a")

    assert gateway.call_counts["describe_subnets"] == 1


def test_resolve_subnets_plural_shares_the_cache() -> None:
    gateway = FakeVpcGateway()
    resolver = NetworkResolver(gateway=gateway)

    subnets = resolver.resolve_subnets(["corp-public-1a", "corp-public-1b", "corp-private-1a"])

    assert len(subnets) == 3
    assert gateway.call_counts["describe_subnets"] == 1


def test_resolving_vpc_twice_costs_exactly_one_describe_vpcs_call() -> None:
    gateway = FakeVpcGateway()
    resolver = NetworkResolver(gateway=gateway)

    resolver.resolve_vpc(VPC_ID)
    resolver.resolve_vpc("corp-main-vpc")

    assert gateway.call_counts["describe_vpcs"] == 1

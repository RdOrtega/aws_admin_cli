"""Tests for VPC domain models: hydration from real boto3-shaped responses, and computed fields."""

import pytest
from aws_admin_cli.domain.models.vpc import (
    IpPermission,
    RouteTable,
    SecurityGroup,
    Subnet,
    Vpc,
    tag_value,
)

# -- tag_value ---------------------------------------------------------------


def test_tag_value_returns_the_matching_value() -> None:
    tags = [{"Key": "Name", "Value": "corp-main-vpc"}, {"Key": "Env", "Value": "prod"}]
    assert tag_value(tags, "Name") == "corp-main-vpc"
    assert tag_value(tags, "Env") == "prod"


def test_tag_value_returns_none_when_key_absent() -> None:
    tags = [{"Key": "Env", "Value": "prod"}]
    assert tag_value(tags, "Name") is None


def test_tag_value_returns_none_for_empty_tags() -> None:
    assert tag_value([], "Name") is None


# -- Vpc hydration -------------------------------------------------------------


def test_vpc_hydrates_from_a_real_describe_vpcs_entry() -> None:
    raw = {
        "VpcId": "vpc-0a1b2c3d",
        "CidrBlock": "10.0.0.0/16",
        "State": "available",
        "IsDefault": False,
        "Tags": [{"Key": "Name", "Value": "corp-main-vpc"}],
    }
    vpc = Vpc.model_validate(raw)
    assert vpc.vpc_id == "vpc-0a1b2c3d"
    assert vpc.name == "corp-main-vpc"
    assert vpc.display_name == "corp-main-vpc"


def test_vpc_display_name_falls_back_to_id_without_a_name_tag() -> None:
    vpc = Vpc(vpc_id="vpc-0a1b2c3d", cidr_block="10.0.0.0/16", state="available")
    assert vpc.name is None
    assert vpc.display_name == "vpc-0a1b2c3d"


# -- Subnet hydration -----------------------------------------------------------


def test_subnet_hydrates_from_a_real_describe_subnets_entry() -> None:
    raw = {
        "SubnetId": "subnet-0a1b2c01",
        "VpcId": "vpc-0a1b2c3d",
        "CidrBlock": "10.0.1.0/24",
        "AvailabilityZone": "us-east-1a",
        "AvailableIpAddressCount": 251,
        "MapPublicIpOnLaunch": True,
        "State": "available",
        "Tags": [{"Key": "Name", "Value": "corp-public-1a"}],
    }
    subnet = Subnet.model_validate(raw)
    assert subnet.subnet_id == "subnet-0a1b2c01"
    assert subnet.name == "corp-public-1a"
    assert subnet.map_public_ip_on_launch is True
    # is_public is never in the raw AWS response -- only the gateway fills it in.
    assert subnet.is_public is None


def test_subnet_display_name_falls_back_to_id_without_a_name_tag() -> None:
    subnet = Subnet(
        subnet_id="subnet-0a1b2c01",
        vpc_id="vpc-0a1b2c3d",
        cidr_block="10.0.1.0/24",
        availability_zone="us-east-1a",
        available_ip_address_count=251,
        state="available",
    )
    assert subnet.display_name == "subnet-0a1b2c01"


# -- IpPermission flattening -----------------------------------------------------


def test_ip_permission_flattens_ip_ranges_ipv6_ranges_and_group_pairs() -> None:
    raw = {
        "IpProtocol": "tcp",
        "FromPort": 22,
        "ToPort": 22,
        "IpRanges": [{"CidrIp": "0.0.0.0/0"}, {"CidrIp": "10.0.0.0/8"}],
        "Ipv6Ranges": [{"CidrIpv6": "::/0"}],
        "UserIdGroupPairs": [{"GroupId": "sg-0a1b2c01"}],
        "PrefixListIds": [{"PrefixListId": "pl-0a1b2c01"}],
    }
    permission = IpPermission.model_validate(raw)
    assert permission.ip_ranges == ["0.0.0.0/0", "10.0.0.0/8"]
    assert permission.ipv6_ranges == ["::/0"]
    assert permission.source_group_ids == ["sg-0a1b2c01"]
    assert permission.prefix_list_ids == ["pl-0a1b2c01"]


# -- port_range_display ---------------------------------------------------------


@pytest.mark.parametrize(
    ("ip_protocol", "from_port", "to_port", "expected"),
    [
        ("tcp", 22, 22, "22"),
        ("tcp", 1024, 65535, "1024-65535"),
        ("tcp", None, None, "ALL"),
        ("-1", None, None, "ALL"),
    ],
)
def test_port_range_display(
    ip_protocol: str, from_port: int | None, to_port: int | None, expected: str
) -> None:
    permission = IpPermission(ip_protocol=ip_protocol, from_port=from_port, to_port=to_port)
    assert permission.port_range_display == expected


# -- open_to_world ----------------------------------------------------------------


def test_open_to_world_true_for_ipv4_world_cidr() -> None:
    permission = IpPermission(ip_protocol="tcp", from_port=22, to_port=22, ip_ranges=["0.0.0.0/0"])
    assert permission.open_to_world is True


def test_open_to_world_true_for_ipv6_world_cidr() -> None:
    permission = IpPermission(ip_protocol="tcp", from_port=22, to_port=22, ipv6_ranges=["::/0"])
    assert permission.open_to_world is True


def test_open_to_world_false_for_private_cidr() -> None:
    permission = IpPermission(
        ip_protocol="tcp", from_port=22, to_port=22, ip_ranges=["10.0.0.0/16"]
    )
    assert permission.open_to_world is False


# -- RouteTable.has_igw_route -----------------------------------------------------


def test_has_igw_route_true_when_default_route_points_at_an_igw() -> None:
    table = RouteTable(
        route_table_id="rtb-0a1b2c3d",
        vpc_id="vpc-0a1b2c3d",
        routes=[
            {"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"},
            {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0a1b2c3d"},
        ],
    )
    assert table.has_igw_route is True


def test_has_igw_route_false_without_a_default_igw_route() -> None:
    table = RouteTable(
        route_table_id="rtb-0a1b2c3e",
        vpc_id="vpc-0a1b2c3d",
        routes=[{"DestinationCidrBlock": "10.0.0.0/16", "GatewayId": "local"}],
    )
    assert table.has_igw_route is False


def test_has_igw_route_false_when_default_route_points_at_a_nat_gateway() -> None:
    table = RouteTable(
        route_table_id="rtb-0a1b2c3f",
        vpc_id="vpc-0a1b2c3d",
        routes=[{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "nat-0a1b2c3d"}],
    )
    assert table.has_igw_route is False


def test_associated_subnet_ids_and_is_main() -> None:
    table = RouteTable(
        route_table_id="rtb-0a1b2c3d",
        vpc_id="vpc-0a1b2c3d",
        associations=[{"SubnetId": "subnet-0a1b2c01"}, {"SubnetId": "subnet-0a1b2c02"}],
    )
    assert table.associated_subnet_ids == ["subnet-0a1b2c01", "subnet-0a1b2c02"]
    assert table.is_main is False

    main_table = RouteTable(
        route_table_id="rtb-0a1b2c3e", vpc_id="vpc-0a1b2c3d", associations=[{"Main": True}]
    )
    assert main_table.is_main is True
    assert main_table.associated_subnet_ids == []


# -- SecurityGroup hydration -------------------------------------------------------


def test_security_group_hydrates_ingress_and_egress() -> None:
    raw = {
        "GroupId": "sg-0a1b2c02",
        "GroupName": "corp-bastion-sg",
        "VpcId": "vpc-0a1b2c3d",
        "Description": "HALLAZGO CRITICO: SSH abierto al mundo",
        "IpPermissions": [
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
        "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
    }
    sg = SecurityGroup.model_validate(raw)
    assert sg.group_id == "sg-0a1b2c02"
    assert len(sg.ingress) == 1
    assert sg.ingress[0].open_to_world is True
    assert len(sg.egress) == 1
    assert sg.egress[0].is_all_protocols is True
    assert sg.display_name == "corp-bastion-sg"  # no Name tag -- falls back to GroupName

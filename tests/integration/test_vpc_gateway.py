"""Integration tests for Boto3VpcGateway, against moto (no real AWS or LocalStack).

The network topology is created via a plain boto3 ``ec2`` client directly in
the fixtures below -- that's test SETUP, not the CLI, and it doesn't violate
the vpc module's read-only rule: ``Boto3VpcGateway`` itself, exercised by
every test in this file, never issues a single mutating call (see
``tests/unit/architecture/test_vpc_readonly.py``).
"""

from collections.abc import Iterator

import boto3
import pytest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.gateways.boto3_vpc_gateway import Boto3VpcGateway
from aws_admin_cli.infrastructure.aws.session_factory import Boto3SessionFactory
from moto import mock_aws
from mypy_boto3_ec2.client import EC2Client


@pytest.fixture
def gateway() -> Boto3VpcGateway:
    settings = Settings(profile="testprofile", region="us-east-1")
    client_factory = ClientFactory(
        session_factory=Boto3SessionFactory(profile=settings.profile, region=settings.region),
        settings=settings,
    )
    return Boto3VpcGateway(client_factory=client_factory)


@pytest.fixture
def ec2_client() -> Iterator[EC2Client]:
    with mock_aws():
        yield boto3.client(
            "ec2",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )


def _seed_bootstrap_topology(client: EC2Client) -> dict[str, str]:
    """Replicate localstack/init/01-bootstrap.sh's topology directly via boto3.

    Setup for these tests, not the CLI under test -- see this module's
    docstring.
    """
    vpc_id = client.create_vpc(
        CidrBlock="10.0.0.0/16",
        TagSpecifications=[
            {"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "corp-main-vpc"}]}
        ],
    )["Vpc"]["VpcId"]

    public_1a = client.create_subnet(
        VpcId=vpc_id,
        CidrBlock="10.0.1.0/24",
        AvailabilityZone="us-east-1a",
        TagSpecifications=[
            {"ResourceType": "subnet", "Tags": [{"Key": "Name", "Value": "corp-public-1a"}]}
        ],
    )["Subnet"]["SubnetId"]
    client.modify_subnet_attribute(SubnetId=public_1a, MapPublicIpOnLaunch={"Value": True})

    private_1a = client.create_subnet(
        VpcId=vpc_id,
        CidrBlock="10.0.11.0/24",
        AvailabilityZone="us-east-1a",
        TagSpecifications=[
            {"ResourceType": "subnet", "Tags": [{"Key": "Name", "Value": "corp-private-1a"}]}
        ],
    )["Subnet"]["SubnetId"]

    # A third subnet, deliberately left with NO explicit route-table association --
    # it must inherit the VPC's main route table (private, no IGW route).
    unassociated = client.create_subnet(
        VpcId=vpc_id,
        CidrBlock="10.0.12.0/24",
        AvailabilityZone="us-east-1b",
        TagSpecifications=[
            {"ResourceType": "subnet", "Tags": [{"Key": "Name", "Value": "corp-unassociated"}]}
        ],
    )["Subnet"]["SubnetId"]

    igw_id = client.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    client.attach_internet_gateway(VpcId=vpc_id, InternetGatewayId=igw_id)

    public_rtb_id = client.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    client.create_route(
        RouteTableId=public_rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id
    )
    client.associate_route_table(RouteTableId=public_rtb_id, SubnetId=public_1a)

    sg_id = client.create_security_group(
        GroupName="corp-bastion-sg", Description="SSH abierto al mundo", VpcId=vpc_id
    )["GroupId"]
    client.authorize_security_group_ingress(
        GroupId=sg_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )

    return {
        "vpc_id": vpc_id,
        "public_1a": public_1a,
        "private_1a": private_1a,
        "unassociated": unassociated,
        "sg_id": sg_id,
    }


def test_describe_vpcs_returns_the_seeded_vpc(
    gateway: Boto3VpcGateway, ec2_client: EC2Client
) -> None:
    ids = _seed_bootstrap_topology(ec2_client)

    vpcs = gateway.describe_vpcs(None, None)

    assert any(v.vpc_id == ids["vpc_id"] and v.name == "corp-main-vpc" for v in vpcs)


def test_describe_subnets_returns_the_seeded_subnets(
    gateway: Boto3VpcGateway, ec2_client: EC2Client
) -> None:
    ids = _seed_bootstrap_topology(ec2_client)

    subnets = gateway.describe_subnets(None, ids["vpc_id"], None)

    assert {s.subnet_id for s in subnets} == {
        ids["public_1a"],
        ids["private_1a"],
        ids["unassociated"],
    }


def test_is_public_true_for_a_subnet_routed_to_an_igw(
    gateway: Boto3VpcGateway, ec2_client: EC2Client
) -> None:
    ids = _seed_bootstrap_topology(ec2_client)

    subnets = {s.subnet_id: s for s in gateway.describe_subnets(None, ids["vpc_id"], None)}

    assert subnets[ids["public_1a"]].is_public is True


def test_is_public_false_for_a_private_subnet(
    gateway: Boto3VpcGateway, ec2_client: EC2Client
) -> None:
    ids = _seed_bootstrap_topology(ec2_client)

    subnets = {s.subnet_id: s for s in gateway.describe_subnets(None, ids["vpc_id"], None)}

    assert subnets[ids["private_1a"]].is_public is False


def test_subnet_without_explicit_association_inherits_the_main_route_table(
    gateway: Boto3VpcGateway, ec2_client: EC2Client
) -> None:
    ids = _seed_bootstrap_topology(ec2_client)

    subnets = {s.subnet_id: s for s in gateway.describe_subnets(None, ids["vpc_id"], None)}

    # The main route table has no IGW route -- so an unassociated subnet is private.
    assert subnets[ids["unassociated"]].is_public is False


def test_describe_security_groups_filters_by_vpc_id(
    gateway: Boto3VpcGateway, ec2_client: EC2Client
) -> None:
    ids = _seed_bootstrap_topology(ec2_client)

    groups = gateway.describe_security_groups(None, ids["vpc_id"], None)

    assert any(g.group_id == ids["sg_id"] and g.group_name == "corp-bastion-sg" for g in groups)
    assert all(g.vpc_id == ids["vpc_id"] for g in groups)


def test_describe_security_groups_is_fully_paginated(
    gateway: Boto3VpcGateway, ec2_client: EC2Client
) -> None:
    ids = _seed_bootstrap_topology(ec2_client)
    for i in range(120):
        ec2_client.create_security_group(
            GroupName=f"paginated-sg-{i:03d}", Description="pagination test", VpcId=ids["vpc_id"]
        )

    groups = gateway.describe_security_groups(None, ids["vpc_id"], None)

    # 120 created here + the bastion SG from the bootstrap topology + the VPC's
    # own default SG that AWS/moto creates automatically.
    assert len(groups) == 122


def test_describe_availability_zones_returns_at_least_one(
    gateway: Boto3VpcGateway, ec2_client: EC2Client
) -> None:
    del ec2_client  # only needed to activate mock_aws for this test
    zones = gateway.describe_availability_zones()
    assert len(zones) >= 1
    assert all(zone.zone_name.startswith("us-east-1") for zone in zones)

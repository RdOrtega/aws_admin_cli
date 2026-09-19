"""Integration tests for the `vpc` CLI commands, via CliRunner + moto.

Network setup uses a plain boto3 ``ec2`` client directly (test setup, not the
CLI) -- see ``tests/integration/test_vpc_gateway.py``'s module docstring for
why that's not a Separation-of-Duties violation.
"""

import json
from typing import Any

import boto3
from aws_admin_cli.main import app
from moto import mock_aws
from typer.testing import CliRunner

_BASE_ARGS = ["--profile", "testprofile"]


def _ec2_client() -> Any:
    return boto3.client(
        "ec2", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing"
    )


def _seed_vpc_with_subnets() -> tuple[str, str, str]:
    client = _ec2_client()
    vpc_id = client.create_vpc(
        CidrBlock="10.0.0.0/16",
        TagSpecifications=[
            {"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "corp-main-vpc"}]}
        ],
    )["Vpc"]["VpcId"]

    public_id = client.create_subnet(
        VpcId=vpc_id,
        CidrBlock="10.0.1.0/24",
        AvailabilityZone="us-east-1a",
        TagSpecifications=[
            {"ResourceType": "subnet", "Tags": [{"Key": "Name", "Value": "corp-public-1a"}]}
        ],
    )["Subnet"]["SubnetId"]

    igw_id = client.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    client.attach_internet_gateway(VpcId=vpc_id, InternetGatewayId=igw_id)
    rtb_id = client.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
    client.create_route(RouteTableId=rtb_id, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    client.associate_route_table(RouteTableId=rtb_id, SubnetId=public_id)

    private_id = client.create_subnet(
        VpcId=vpc_id,
        CidrBlock="10.0.11.0/24",
        AvailabilityZone="us-east-1a",
        TagSpecifications=[
            {"ResourceType": "subnet", "Tags": [{"Key": "Name", "Value": "corp-private-1a"}]}
        ],
    )["Subnet"]["SubnetId"]

    return vpc_id, public_id, private_id


@mock_aws
def test_vpc_list_json_output_is_parseable(cli_runner: CliRunner) -> None:
    _seed_vpc_with_subnets()

    result = cli_runner.invoke(app, [*_BASE_ARGS, "--output", "json", "vpc", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert any(vpc["Tags"] for vpc in payload if vpc.get("VpcId"))


@mock_aws
def test_sg_audit_json_output_lists_findings_and_exits_zero(cli_runner: CliRunner) -> None:
    vpc_id, _public_id, _private_id = _seed_vpc_with_subnets()
    client = _ec2_client()
    client.authorize_security_group_ingress(
        GroupId=client.create_security_group(
            GroupName="corp-bastion-sg", Description="ssh open", VpcId=vpc_id
        )["GroupId"],
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )

    result = cli_runner.invoke(app, [*_BASE_ARGS, "--output", "json", "vpc", "sg", "audit"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert any(finding["RuleId"] == "SG001" for finding in payload)


@mock_aws
def test_sg_audit_fail_on_findings_exits_three_when_ssh_is_open(cli_runner: CliRunner) -> None:
    vpc_id, _public_id, _private_id = _seed_vpc_with_subnets()
    client = _ec2_client()
    sg_id = client.create_security_group(
        GroupName="corp-bastion-sg", Description="ssh open", VpcId=vpc_id
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

    result = cli_runner.invoke(app, [*_BASE_ARGS, "vpc", "sg", "audit", "--fail-on-findings"])

    assert result.exit_code == 3, result.output


@mock_aws
def test_sg_audit_fail_on_findings_exits_zero_on_a_clean_network(cli_runner: CliRunner) -> None:
    _seed_vpc_with_subnets()  # no security groups created beyond the VPC's own default SG

    result = cli_runner.invoke(
        app,
        [
            *_BASE_ARGS,
            "vpc",
            "sg",
            "audit",
            "--min-severity",
            "CRITICAL",
            "--fail-on-findings",
        ],
    )

    assert result.exit_code == 0, result.output


@mock_aws
def test_vpc_resolve_subnet_by_name_prints_the_correct_id(cli_runner: CliRunner) -> None:
    _vpc_id, public_id, _private_id = _seed_vpc_with_subnets()

    result = cli_runner.invoke(
        app,
        [
            *_BASE_ARGS,
            "--output",
            "json",
            "vpc",
            "resolve",
            "--vpc",
            "corp-main-vpc",
            "--subnet",
            "corp-public-1a",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    resolved = next(row for row in payload if row["Type"] == "subnet")
    assert resolved["ResolvedId"] == public_id


@mock_aws
def test_subnet_list_public_filters_correctly(cli_runner: CliRunner) -> None:
    _vpc_id, public_id, private_id = _seed_vpc_with_subnets()

    result = cli_runner.invoke(
        app, [*_BASE_ARGS, "--output", "json", "vpc", "subnet", "list", "--public"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    ids = {row["SubnetId"] for row in payload}
    assert public_id in ids
    assert private_id not in ids

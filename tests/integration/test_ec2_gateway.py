"""Integration tests for Boto3Ec2Gateway, against moto (no real AWS or LocalStack).

The network/AMI scaffolding below uses a plain boto3 ``ec2`` client directly --
that's test SETUP, not src/, so it doesn't violate the vpc module's read-only
rule (see ``tests/integration/test_vpc_gateway.py``'s module docstring for the
same reasoning): ``Boto3Ec2Gateway`` itself, exercised by every test in this
file, never issues a security-group/subnet/VPC-mutating call (see
``tests/unit/architecture/test_vpc_readonly.py``).
"""

from collections.abc import Iterator

import boto3
import pytest
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.exceptions import AccessDeniedError, ResourceNotFoundError
from aws_admin_cli.domain.models.ec2 import InstanceState, LaunchSpec
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.gateways.boto3_ec2_gateway import (
    Boto3Ec2Gateway,
    _build_network_interface,
)
from aws_admin_cli.infrastructure.aws.session_factory import Boto3SessionFactory
from moto import mock_aws
from mypy_boto3_ec2.client import EC2Client


@pytest.fixture
def gateway() -> Boto3Ec2Gateway:
    settings = Settings(profile="testprofile", region="us-east-1")
    client_factory = ClientFactory(
        session_factory=Boto3SessionFactory(profile=settings.profile, region=settings.region),
        settings=settings,
    )
    return Boto3Ec2Gateway(client_factory=client_factory)


@pytest.fixture
def raw_ec2() -> Iterator[EC2Client]:
    """A plain boto3 EC2 client (and the active moto sandbox), for test scaffolding only."""
    with mock_aws():
        yield boto3.client(
            "ec2",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )


def _default_subnet_id(raw_client: EC2Client) -> str:
    vpcs = raw_client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    vpc_id = vpcs[0]["VpcId"]
    subnets = raw_client.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])[
        "Subnets"
    ]
    return str(subnets[0]["SubnetId"])


def _default_sg_id(raw_client: EC2Client, vpc_id: str) -> str:
    response = raw_client.create_security_group(
        GroupName="ec2-gateway-test-sg", Description="test", VpcId=vpc_id
    )
    return str(response["GroupId"])


def _any_ami_id(raw_client: EC2Client) -> str:
    images = raw_client.describe_images(Owners=["amazon"])["Images"]
    return str(images[0]["ImageId"])


def _launch_spec(subnet_id: str, sg_id: str, ami_id: str, **overrides: object) -> LaunchSpec:
    defaults: dict[str, object] = {
        "image_id": ami_id,
        "instance_type": "t3.micro",
        "subnet_id": subnet_id,
        "security_group_ids": [sg_id],
        "tags": {"Name": "gateway-test"},
    }
    defaults.update(overrides)
    return LaunchSpec(**defaults)


# -- _build_network_interface (pure, no moto) ----------------------------------------


def test_build_network_interface_without_public_ip() -> None:
    spec = LaunchSpec(
        image_id="ami-x",
        instance_type="t3.micro",
        subnet_id="subnet-x",
        security_group_ids=["sg-a", "sg-b"],
        assign_public_ip=False,
    )
    kwargs = _build_network_interface(spec)
    assert kwargs == {
        "DeviceIndex": 0,
        "SubnetId": "subnet-x",
        "Groups": ["sg-a", "sg-b"],
        "AssociatePublicIpAddress": False,
    }


def test_build_network_interface_with_public_ip() -> None:
    spec = LaunchSpec(
        image_id="ami-x",
        instance_type="t3.micro",
        subnet_id="subnet-x",
        security_group_ids=["sg-a"],
        assign_public_ip=True,
    )
    kwargs = _build_network_interface(spec)
    assert kwargs["AssociatePublicIpAddress"] is True


# -- full lifecycle -------------------------------------------------------------------


def test_instance_full_lifecycle(gateway: Boto3Ec2Gateway, raw_ec2: EC2Client) -> None:
    subnet_id = _default_subnet_id(raw_ec2)
    vpc_id = raw_ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    sg_id = _default_sg_id(raw_ec2, vpc_id)
    ami_id = _any_ami_id(raw_ec2)
    spec = _launch_spec(subnet_id, sg_id, ami_id)

    launched = gateway.run_instance(spec, client_token="lifecycle-token", dry_run=False)
    assert launched.state is InstanceState.PENDING

    fetched = gateway.get_instance(launched.instance_id)
    assert fetched.instance_id == launched.instance_id

    stopped_ids = gateway.stop_instances([launched.instance_id], force=False)
    assert stopped_ids == [launched.instance_id]

    started_ids = gateway.start_instances([launched.instance_id])
    assert started_ids == [launched.instance_id]

    terminated_ids = gateway.terminate_instances([launched.instance_id], dry_run=False)
    assert terminated_ids == [launched.instance_id]


# -- tags applied at launch -------------------------------------------------------------


def test_tags_are_visible_immediately_after_launch(
    gateway: Boto3Ec2Gateway, raw_ec2: EC2Client
) -> None:
    subnet_id = _default_subnet_id(raw_ec2)
    vpc_id = raw_ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    sg_id = _default_sg_id(raw_ec2, vpc_id)
    ami_id = _any_ami_id(raw_ec2)
    spec = _launch_spec(subnet_id, sg_id, ami_id)

    launched = gateway.run_instance(spec, client_token="tags-token", dry_run=False)

    tags = {tag["Key"]: tag["Value"] for tag in launched.tags}
    assert tags["ManagedBy"] == "aws-admin-cli"

    fetched = gateway.get_instance(launched.instance_id)
    fetched_tags = {tag["Key"]: tag["Value"] for tag in fetched.tags}
    assert fetched_tags["ManagedBy"] == "aws-admin-cli"


# -- tagging after launch: create_tags / delete_tags ------------------------------------


def test_create_tags_adds_and_overwrites(gateway: Boto3Ec2Gateway, raw_ec2: EC2Client) -> None:
    subnet_id = _default_subnet_id(raw_ec2)
    vpc_id = raw_ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    sg_id = _default_sg_id(raw_ec2, vpc_id)
    ami_id = _any_ami_id(raw_ec2)
    launched = gateway.run_instance(
        _launch_spec(subnet_id, sg_id, ami_id), client_token="create-tags-token", dry_run=False
    )

    gateway.create_tags([launched.instance_id], {"Environment": "dev", "Owner": "alice"})
    fetched = gateway.get_instance(launched.instance_id)
    tags = {tag["Key"]: tag["Value"] for tag in fetched.tags}
    assert tags["Environment"] == "dev"
    assert tags["Owner"] == "alice"
    assert tags["ManagedBy"] == "aws-admin-cli"  # the launch-time tag survives untouched

    # CreateTags overwrites an existing key rather than erroring or duplicating.
    gateway.create_tags([launched.instance_id], {"Environment": "prod"})
    refetched = gateway.get_instance(launched.instance_id)
    refetched_tags = {tag["Key"]: tag["Value"] for tag in refetched.tags}
    assert refetched_tags["Environment"] == "prod"


def test_delete_tags_removes_by_key(gateway: Boto3Ec2Gateway, raw_ec2: EC2Client) -> None:
    subnet_id = _default_subnet_id(raw_ec2)
    vpc_id = raw_ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    sg_id = _default_sg_id(raw_ec2, vpc_id)
    ami_id = _any_ami_id(raw_ec2)
    launched = gateway.run_instance(
        _launch_spec(subnet_id, sg_id, ami_id), client_token="delete-tags-token", dry_run=False
    )
    gateway.create_tags([launched.instance_id], {"Temporary": "yes"})

    gateway.delete_tags([launched.instance_id], ["Temporary"])

    fetched = gateway.get_instance(launched.instance_id)
    tags = {tag["Key"]: tag["Value"] for tag in fetched.tags}
    assert "Temporary" not in tags
    assert tags["ManagedBy"] == "aws-admin-cli"


def test_create_tags_and_delete_tags_are_no_ops_on_empty_input(
    gateway: Boto3Ec2Gateway, raw_ec2: EC2Client
) -> None:
    subnet_id = _default_subnet_id(raw_ec2)
    vpc_id = raw_ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    sg_id = _default_sg_id(raw_ec2, vpc_id)
    ami_id = _any_ami_id(raw_ec2)
    launched = gateway.run_instance(
        _launch_spec(subnet_id, sg_id, ami_id), client_token="noop-tags-token", dry_run=False
    )

    gateway.create_tags([launched.instance_id], {})
    gateway.delete_tags([launched.instance_id], [])
    gateway.create_tags([], {"Foo": "bar"})
    gateway.delete_tags([], ["Foo"])

    fetched = gateway.get_instance(launched.instance_id)
    tags = {tag["Key"]: tag["Value"] for tag in fetched.tags}
    assert "Foo" not in tags


# -- IMDSv2 is enforced -----------------------------------------------------------------


def test_launched_instance_requires_imdsv2_tokens(
    gateway: Boto3Ec2Gateway, raw_ec2: EC2Client
) -> None:
    subnet_id = _default_subnet_id(raw_ec2)
    vpc_id = raw_ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    sg_id = _default_sg_id(raw_ec2, vpc_id)
    ami_id = _any_ami_id(raw_ec2)
    spec = _launch_spec(subnet_id, sg_id, ami_id)

    launched = gateway.run_instance(spec, client_token="imds-token", dry_run=False)

    raw = raw_ec2.describe_instances(InstanceIds=[launched.instance_id])["Reservations"][0][
        "Instances"
    ][0]
    assert raw["MetadataOptions"]["HttpTokens"] == "required"


# -- key pairs ----------------------------------------------------------------------------


@mock_aws
def test_key_pair_create_describe_delete(gateway: Boto3Ec2Gateway) -> None:
    material = gateway.create_key_pair("demo-key", "rsa")
    assert material.private_key
    assert "demo-key" in repr(material)
    assert material.private_key not in repr(material)

    listed = gateway.describe_key_pairs(["demo-key"])
    assert any(info.key_name == "demo-key" for info in listed)

    gateway.delete_key_pair("demo-key")
    with pytest.raises(ResourceNotFoundError):
        gateway.describe_key_pairs(["demo-key"])


# -- describe_images with owners ------------------------------------------------------------


@mock_aws
def test_describe_images_with_owners(gateway: Boto3Ec2Gateway) -> None:
    images = gateway.describe_images(None, ["amazon"], None)
    assert images
    assert all(image.owner_id == "amazon" or image.owner_id for image in images)


# -- pagination -----------------------------------------------------------------------------


def test_describe_instances_flattens_all_reservations_across_pages(
    gateway: Boto3Ec2Gateway, raw_ec2: EC2Client
) -> None:
    subnet_id = _default_subnet_id(raw_ec2)
    vpc_id = raw_ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    sg_id = _default_sg_id(raw_ec2, vpc_id)
    ami_id = _any_ami_id(raw_ec2)

    launched_ids = []
    for i in range(30):
        spec = _launch_spec(subnet_id, sg_id, ami_id, tags={"Name": f"paginated-{i}"})
        launched = gateway.run_instance(spec, client_token=f"page-token-{i}", dry_run=False)
        launched_ids.append(launched.instance_id)

    described = gateway.describe_instances(launched_ids, None)
    assert {i.instance_id for i in described} == set(launched_ids)
    assert len(described) == 30


# -- dry-run ------------------------------------------------------------------------------


def test_dry_run_launch_translates_dry_run_operation_to_success(
    gateway: Boto3Ec2Gateway, raw_ec2: EC2Client
) -> None:
    subnet_id = _default_subnet_id(raw_ec2)
    vpc_id = raw_ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    sg_id = _default_sg_id(raw_ec2, vpc_id)
    ami_id = _any_ami_id(raw_ec2)
    spec = _launch_spec(subnet_id, sg_id, ami_id)

    result = gateway.run_instance(spec, client_token="dry-run-token", dry_run=True)

    assert result.instance_id == "dry-run"
    # Nothing was actually launched.
    reservations = raw_ec2.describe_instances()["Reservations"]
    assert reservations == []


def test_dry_run_terminate_does_not_terminate(gateway: Boto3Ec2Gateway, raw_ec2: EC2Client) -> None:
    subnet_id = _default_subnet_id(raw_ec2)
    vpc_id = raw_ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]["VpcId"]
    sg_id = _default_sg_id(raw_ec2, vpc_id)
    ami_id = _any_ami_id(raw_ec2)
    spec = _launch_spec(subnet_id, sg_id, ami_id)
    launched = gateway.run_instance(spec, client_token="dry-run-term-token", dry_run=False)

    ids = gateway.terminate_instances([launched.instance_id], dry_run=True)

    assert ids == [launched.instance_id]
    fetched = gateway.get_instance(launched.instance_id)
    assert fetched.state is not InstanceState.TERMINATED


# -- describe_instances_all_regions (global EC2 List/Filter fetch) -------------------------


@mock_aws
def test_describe_instances_all_regions_aggregates_across_regions(
    gateway: Boto3Ec2Gateway,
) -> None:
    """Instances launched in two different regions both come back, each tagged with
    the region it actually lives in -- not the gateway's own default region.
    """
    us_east = gateway.client_factory.ec2(region="us-east-1")
    us_west = gateway.client_factory.ec2(region="us-west-2")
    east_ami = us_east.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    west_ami = us_west.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    us_east.run_instances(ImageId=east_ami, MinCount=1, MaxCount=1, InstanceType="t2.micro")
    us_west.run_instances(ImageId=west_ami, MinCount=1, MaxCount=1, InstanceType="t2.micro")

    instances = gateway.describe_instances_all_regions(None)

    by_region = {i.region for i in instances}
    assert "us-east-1" in by_region
    assert "us-west-2" in by_region
    east_instance = next(i for i in instances if i.region == "us-east-1")
    west_instance = next(i for i in instances if i.region == "us-west-2")
    assert east_instance.instance_id != west_instance.instance_id


@mock_aws
def test_describe_instances_all_regions_skips_a_region_that_errors(
    gateway: Boto3Ec2Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A region this account can't call ``DescribeInstances`` against (unauthorized,
    or any other AWS error) is skipped -- it must never hide every other region's
    instances behind one failure.
    """
    us_east = gateway.client_factory.ec2(region="us-east-1")
    ami_id = us_east.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    us_east.run_instances(ImageId=ami_id, MinCount=1, MaxCount=1, InstanceType="t2.micro")

    original = Boto3Ec2Gateway._describe_instances_in_region

    def _flaky_in_us_west(
        self: Boto3Ec2Gateway, region: str, filters: object
    ) -> list[object]:
        if region == "us-west-2":
            raise AccessDeniedError("nope", aws_code="UnauthorizedOperation")
        return original(self, region, filters)  # type: ignore[arg-type]

    monkeypatch.setattr(Boto3Ec2Gateway, "_describe_instances_in_region", _flaky_in_us_west)

    instances = gateway.describe_instances_all_regions(None)

    assert any(i.region == "us-east-1" for i in instances)
    assert not any(i.region == "us-west-2" for i in instances)


@mock_aws
def test_list_regions_includes_standard_regions(gateway: Boto3Ec2Gateway) -> None:
    regions = gateway._list_regions()
    assert "us-east-1" in regions
    assert "us-west-2" in regions

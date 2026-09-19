"""Tests for ``ensure_demo_security_groups``: local-only, idempotent SG seeding."""

from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.infrastructure.local.sg_seed import ensure_demo_security_groups
from moto import mock_aws


def _local_app_ctx() -> AppContext:
    return AppContext.build(Settings(profile="testprofile", endpoint_url="http://localhost:4566"))


def _default_vpc_id(ctx: AppContext) -> str:
    """Resolve the default VPC through the SAME client ``ensure_demo_security_groups`` uses.

    Deliberately not a raw ``boto3.client()`` call: moto partitions its
    in-memory backend by the client's configured endpoint, so a client built
    with a custom ``endpoint_url`` (as ``ctx.client_factory.ec2()`` is, once
    ``settings.endpoint_url`` is set) does not share state with a client left
    on boto3's default AWS endpoint resolution -- every lookup in a "local"
    test must go through this same ``ctx``-derived client to see consistent state.
    """
    vpcs = ctx.client_factory.ec2().describe_vpcs(
        Filters=[{"Name": "isDefault", "Values": ["true"]}]
    )["Vpcs"]
    return str(vpcs[0]["VpcId"])


@mock_aws
def test_creates_web_db_and_ssh_only_groups() -> None:
    ctx = _local_app_ctx()
    vpc_id = _default_vpc_id(ctx)

    ensure_demo_security_groups(ctx, vpc_id)

    names = {
        sg["GroupName"]
        for sg in ctx.client_factory.ec2().describe_security_groups()["SecurityGroups"]
        if sg.get("VpcId") == vpc_id
    }
    assert {"default", "web-sg", "db-sg", "ssh-only"} <= names


@mock_aws
def test_is_idempotent_on_a_second_call() -> None:
    ctx = _local_app_ctx()
    vpc_id = _default_vpc_id(ctx)

    ensure_demo_security_groups(ctx, vpc_id)
    ensure_demo_security_groups(ctx, vpc_id)

    groups = [
        sg
        for sg in ctx.client_factory.ec2().describe_security_groups()["SecurityGroups"]
        if sg.get("VpcId") == vpc_id and sg.get("GroupName") == "web-sg"
    ]
    assert len(groups) == 1


def test_never_mutates_against_a_non_local_target() -> None:
    """No ``endpoint_url`` -> ``is_local`` is False -> the function must never touch AWS.

    No ``@mock_aws`` here on purpose: ``ctx``'s profile ("testprofile") does
    not exist on this machine, so if ``ensure_demo_security_groups`` ever
    tried to build a real client despite the non-local guard, this would
    fail loudly with ``ProfileNotFoundError`` instead of silently mutating
    something -- the guard itself is what's under test, not moto's state.
    """
    ctx = AppContext.build(Settings(profile="testprofile"))  # no endpoint_url -> not local
    ensure_demo_security_groups(ctx, "vpc-doesnotmatter")

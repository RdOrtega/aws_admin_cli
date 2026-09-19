"""Integration tests for the `ec2` CLI commands, via CliRunner + moto."""

import json
import stat
from pathlib import Path

import boto3
from aws_admin_cli.core.exceptions import AwsAdminCliError
from aws_admin_cli.main import app
from moto import mock_aws
from typer.testing import CliRunner

_BASE_ARGS = ["--profile", "testprofile"]

# See tests/integration/test_iam_cli.py's module docstring/comment: CliRunner bypasses
# main.run()'s sys.exit(exc.exit_code) translation, so result.exit_code is always 1 for
# any raised AwsAdminCliError -- the real per-error exit code lives on result.exception.


def _default_subnet_and_sg() -> tuple[str, str]:
    """Set up a subnet + security group in moto's default VPC, via a raw boto3 client.

    Test scaffolding only -- never exercised by src/, so it's fine for this
    helper (unlike anything under src/aws_admin_cli) to call
    ``create_security_group``.
    """
    client = boto3.client("ec2", region_name="us-east-1")
    vpcs = client.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    vpc_id = vpcs[0]["VpcId"]
    subnets = client.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    subnet_id = subnets[0]["SubnetId"]
    sg = client.create_security_group(
        GroupName="ec2-cli-test-sg", Description="test", VpcId=vpc_id
    )
    return subnet_id, sg["GroupId"]


def _any_ami_id() -> str:
    client = boto3.client("ec2", region_name="us-east-1")
    images = client.describe_images(Owners=["amazon"])["Images"]
    return str(images[0]["ImageId"])


@mock_aws
def test_instance_launch_json_output_is_parseable(cli_runner: CliRunner) -> None:
    subnet_id, sg_id = _default_subnet_and_sg()
    ami_id = _any_ami_id()

    result = cli_runner.invoke(
        app,
        [
            *_BASE_ARGS,
            "--output",
            "json",
            "ec2",
            "instance",
            "launch",
            "demo-web",
            "--ami",
            ami_id,
            "--type",
            "t3.micro",
            "--subnet",
            subnet_id,
            "--sg",
            sg_id,
            "--yes",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["InstanceId"]
    assert payload["ImageId"] == ami_id


@mock_aws
def test_instance_launch_dry_run_creates_nothing(cli_runner: CliRunner) -> None:
    subnet_id, sg_id = _default_subnet_and_sg()
    ami_id = _any_ami_id()

    result = cli_runner.invoke(
        app,
        [
            *_BASE_ARGS,
            "--output",
            "json",
            "ec2",
            "instance",
            "launch",
            "demo-web",
            "--ami",
            ami_id,
            "--type",
            "t3.micro",
            "--subnet",
            subnet_id,
            "--sg",
            sg_id,
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    raw_client = boto3.client("ec2", region_name="us-east-1")
    assert raw_client.describe_instances()["Reservations"] == []


@mock_aws
def test_instance_terminate_unmanaged_without_force_fails_with_exit_64(
    cli_runner: CliRunner,
) -> None:
    subnet_id, sg_id = _default_subnet_and_sg()
    ami_id = _any_ami_id()
    # Launch outside the CLI's own tagging (a raw boto3 call), so the instance
    # is NOT tagged ManagedBy=aws-admin-cli -- exactly like "someone else's instance".
    raw_client = boto3.client("ec2", region_name="us-east-1")
    reservation = raw_client.run_instances(
        ImageId=ami_id,
        InstanceType="t3.micro",
        MinCount=1,
        MaxCount=1,
        SubnetId=subnet_id,
        SecurityGroupIds=[sg_id],
    )
    instance_id = reservation["Instances"][0]["InstanceId"]

    result = cli_runner.invoke(
        app, [*_BASE_ARGS, "ec2", "instance", "terminate", instance_id, "--yes"]
    )

    assert isinstance(result.exception, AwsAdminCliError)
    assert result.exception.exit_code == 64
    assert "--force" in str(result.exception.hint)


@mock_aws
def test_keypair_create_writes_file_with_0600_and_json_never_contains_private_key(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    result = cli_runner.invoke(
        app,
        [
            *_BASE_ARGS,
            "--output",
            "json",
            "ec2",
            "keypair",
            "create",
            "demo-key",
            "--path",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0, result.output
    key_path = tmp_path / "demo-key.pem"
    assert key_path.exists()
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert oct(mode)[-3:] == "600"
    assert "BEGIN RSA PRIVATE KEY" not in result.stdout
    payload = json.loads(result.stdout)
    assert set(payload) == {"KeyName", "KeyPairId", "KeyFingerprint", "Path"}
    assert Path(payload["Path"]) == key_path


@mock_aws
def test_keypair_create_over_existing_file_fails_and_leaves_it_intact(
    cli_runner: CliRunner, tmp_path: Path
) -> None:
    key_path = tmp_path / "demo-key.pem"
    key_path.write_text("ORIGINAL-CONTENT-NOT-A-REAL-KEY")

    result = cli_runner.invoke(
        app,
        [*_BASE_ARGS, "ec2", "keypair", "create", "demo-key", "--path", str(tmp_path)],
    )

    assert isinstance(result.exception, AwsAdminCliError)
    assert result.exception.exit_code == 64
    assert key_path.read_text() == "ORIGINAL-CONTENT-NOT-A-REAL-KEY"

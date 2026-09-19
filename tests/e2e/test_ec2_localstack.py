"""E2E test: the full EC2 launch flow against a real LocalStack (requires `make up`).

The rehearsal for Fase 6: create a role an EC2 instance can assume, a bucket
and a read-only policy scoped to it, attach the policy, resolve the target
subnet (read-only, via the vpc module), launch an instance with --iam-role
and --wait, confirm it came up RUNNING with the right subnet and IAM profile,
then terminate and clean everything up.
"""

import json

import boto3
import pytest
from aws_admin_cli.main import app
from typer.testing import CliRunner

from tests.e2e.conftest import LOCALSTACK_ENDPOINT

_BASE_ARGS = ["--profile", "testprofile", "--endpoint-url", LOCALSTACK_ENDPOINT]
_ROLE_NAME = "e2e-ec2-instance-role"
_POLICY_NAME = "e2e-ec2-read-only-bucket"
_BUCKET_NAME = "e2e-ec2-fixture-bucket"
_INSTANCE_NAME = "e2e-demo-web"
_SUBNET_REF = "corp-private-1a"
_SG_REF = "corp-web-sg"


def _read_only_document() -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:ListBucket"],
                    "Resource": [
                        f"arn:aws:s3:::{_BUCKET_NAME}",
                        f"arn:aws:s3:::{_BUCKET_NAME}/*",
                    ],
                }
            ],
        }
    )


def _any_ami_id() -> str:
    """Grab any AMI id LocalStack's EC2 emulation happens to expose.

    LocalStack/moto's catalog is reduced and fictitious (see
    ``application/services/ami_resolver.py``'s module docstring) -- this
    sidesteps depending on any particular distro alias resolving here.
    """
    client = boto3.client(
        "ec2",
        region_name="us-east-1",
        endpoint_url=LOCALSTACK_ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    images = client.describe_images()["Images"]
    assert images, "LocalStack no expone ninguna AMI -- ¿está bien sembrado el entorno?"
    return str(images[0]["ImageId"])


@pytest.mark.e2e
@pytest.mark.usefixtures("skip_if_localstack_down")
def test_ec2_launch_flow_with_iam_role_against_localstack(cli_runner: CliRunner) -> None:
    policy_arn: str | None = None
    instance_id: str | None = None
    try:
        role_result = cli_runner.invoke(
            app,
            [
                *_BASE_ARGS,
                "--output",
                "json",
                "iam",
                "role",
                "create",
                _ROLE_NAME,
                "--service",
                "ec2.amazonaws.com",
            ],
        )
        assert role_result.exit_code == 0, role_result.output

        bucket_result = cli_runner.invoke(
            app, [*_BASE_ARGS, "--output", "json", "s3", "bucket", "create", _BUCKET_NAME]
        )
        assert bucket_result.exit_code == 0, bucket_result.output

        policy_result = cli_runner.invoke(
            app,
            [
                *_BASE_ARGS,
                "--output",
                "json",
                "iam",
                "policy",
                "create",
                _POLICY_NAME,
                "--document-json",
                _read_only_document(),
            ],
        )
        assert policy_result.exit_code == 0, policy_result.output
        policy_arn = json.loads(policy_result.stdout)["Arn"]

        attach_result = cli_runner.invoke(
            app,
            [*_BASE_ARGS, "iam", "role", "attach-policy", _ROLE_NAME, "--policy-arn", policy_arn],
        )
        assert attach_result.exit_code == 0, attach_result.output

        # Read-only: just confirms the target subnet resolves before launching into it.
        subnet_result = cli_runner.invoke(
            app, [*_BASE_ARGS, "--output", "json", "vpc", "subnet", "show", _SUBNET_REF]
        )
        assert subnet_result.exit_code == 0, subnet_result.output

        launch_result = cli_runner.invoke(
            app,
            [
                *_BASE_ARGS,
                "--output",
                "json",
                "ec2",
                "instance",
                "launch",
                _INSTANCE_NAME,
                "--ami",
                _any_ami_id(),
                "--type",
                "t3.micro",
                "--subnet",
                _SUBNET_REF,
                "--sg",
                _SG_REF,
                "--iam-role",
                _ROLE_NAME,
                "--wait",
                "--yes",
            ],
        )
        assert launch_result.exit_code == 0, launch_result.output
        launched = json.loads(launch_result.stdout)
        instance_id = launched["InstanceId"]

        show_result = cli_runner.invoke(
            app, [*_BASE_ARGS, "--output", "json", "ec2", "instance", "show", instance_id]
        )
        assert show_result.exit_code == 0, show_result.output
        shown = json.loads(show_result.stdout)
        assert shown["State"] == "running"
        assert shown["IamInstanceProfileArn"]
        assert _ROLE_NAME in shown["IamInstanceProfileArn"]

        terminate_result = cli_runner.invoke(
            app,
            [
                *_BASE_ARGS,
                "--output",
                "json",
                "ec2",
                "instance",
                "terminate",
                instance_id,
                "--wait",
                "--yes",
            ],
        )
        assert terminate_result.exit_code == 0, terminate_result.output
        terminated = json.loads(terminate_result.stdout)
        assert terminated["State"] == "terminated"
        instance_id = None
    finally:
        if instance_id is not None:
            cli_runner.invoke(
                app, [*_BASE_ARGS, "ec2", "instance", "terminate", instance_id, "--yes"]
            )
        if policy_arn is not None:
            cli_runner.invoke(
                app,
                [
                    *_BASE_ARGS,
                    "iam",
                    "role",
                    "detach-policy",
                    _ROLE_NAME,
                    "--policy-arn",
                    policy_arn,
                ],
            )
            cli_runner.invoke(app, [*_BASE_ARGS, "iam", "policy", "delete", policy_arn, "--yes"])
        # The launch step (--iam-role) creates an instance profile wrapping the role
        # (ensure_instance_profile_for_role.py, Fase 5) -- AWS refuses DeleteRole with
        # DeleteConflict while the role is still attached to any instance profile, so
        # that has to go first. Same name as the role: EnsureInstanceProfileForRoleUseCase's
        # 1:1 naming convention.
        cli_runner.invoke(
            app, [*_BASE_ARGS, "iam", "instance-profile", "delete", _ROLE_NAME, "--yes", "--force"]
        )
        cli_runner.invoke(
            app, [*_BASE_ARGS, "iam", "role", "delete", _ROLE_NAME, "--yes", "--force"]
        )
        cli_runner.invoke(app, [*_BASE_ARGS, "s3", "bucket", "delete", _BUCKET_NAME, "--yes"])

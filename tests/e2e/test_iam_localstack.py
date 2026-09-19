"""E2E test: full IAM flow against a real LocalStack (requires `make up`).

This is the rehearsal for Fase 5 (EC2): create a role an EC2 instance could
assume, a read-only policy scoped to one (fictional) bucket, attach it,
verify it shows up, then tear everything down.
"""

import json

import pytest
from aws_admin_cli.main import app
from typer.testing import CliRunner

from tests.e2e.conftest import LOCALSTACK_ENDPOINT

_BASE_ARGS = ["--profile", "testprofile", "--endpoint-url", LOCALSTACK_ENDPOINT]
_ROLE_NAME = "e2e-ec2-role"
_POLICY_NAME = "e2e-read-only-bucket"
_READ_ONLY_DOCUMENT = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    "arn:aws:s3:::e2e-fixture-bucket",
                    "arn:aws:s3:::e2e-fixture-bucket/*",
                ],
            }
        ],
    }
)


@pytest.mark.e2e
@pytest.mark.usefixtures("skip_if_localstack_down")
def test_iam_role_policy_flow_against_localstack(cli_runner: CliRunner) -> None:
    policy_arn: str | None = None
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
        assert json.loads(role_result.stdout)["RoleName"] == _ROLE_NAME

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
                _READ_ONLY_DOCUMENT,
            ],
        )
        assert policy_result.exit_code == 0, policy_result.output
        policy_arn = json.loads(policy_result.stdout)["Arn"]

        attach_result = cli_runner.invoke(
            app,
            [*_BASE_ARGS, "iam", "role", "attach-policy", _ROLE_NAME, "--policy-arn", policy_arn],
        )
        assert attach_result.exit_code == 0, attach_result.output

        list_result = cli_runner.invoke(
            app, [*_BASE_ARGS, "--output", "json", "iam", "role", "policies", _ROLE_NAME]
        )
        assert list_result.exit_code == 0, list_result.output
        attached = json.loads(list_result.stdout)
        assert any(policy["PolicyArn"] == policy_arn for policy in attached)
    finally:
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
        cli_runner.invoke(
            app, [*_BASE_ARGS, "iam", "role", "delete", _ROLE_NAME, "--yes", "--force"]
        )

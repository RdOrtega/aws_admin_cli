"""Integration tests for StackEngine, against moto (no real AWS or LocalStack).

Real ``Boto3IamGateway``/``Boto3S3Gateway`` (through the real use cases and
steps), exercised end to end: a manifest applied for real against moto,
then destroyed, and a real rollback scenario -- a manifest whose last
resource references an ARN that doesn't exist, verifying the earlier
resources it created are cleaned up.
"""

import logging
from pathlib import Path

import boto3
import pytest
from aws_admin_cli.application.stacks.engine import StackEngine
from aws_admin_cli.application.stacks.registry import StepDependencies, build_steps
from aws_admin_cli.core.config import Settings
from aws_admin_cli.core.exceptions import StackApplyError
from aws_admin_cli.domain.models.stack import ResourceKind, ResourceSpec, StackManifest, StackStatus
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.gateways.boto3_iam_gateway import Boto3IamGateway
from aws_admin_cli.infrastructure.aws.gateways.boto3_s3_gateway import Boto3S3Gateway
from aws_admin_cli.infrastructure.aws.session_factory import Boto3SessionFactory
from botocore.exceptions import ClientError
from moto import mock_aws
from mypy_boto3_iam.client import IAMClient
from mypy_boto3_s3.client import S3Client

from tests.fakes.ec2 import FakeEc2Gateway
from tests.fakes.iam import InMemoryRepository
from tests.fakes.stack import InMemoryStackRepository
from tests.fakes.vpc import FakeVpcGateway

_PROFILE = "testprofile"
_REGION = "us-east-1"


def _resource(
    resource_id: str,
    kind: ResourceKind,
    properties: dict[str, object],
    depends_on: list[str] | None = None,
) -> ResourceSpec:
    return ResourceSpec(
        id=resource_id, kind=kind, properties=properties, depends_on=depends_on or []
    )


def _engine() -> StackEngine:
    settings = Settings(profile=_PROFILE, region=_REGION)
    client_factory = ClientFactory(
        session_factory=Boto3SessionFactory(profile=settings.profile, region=settings.region),
        settings=settings,
    )
    deps = StepDependencies(
        iam_gateway=Boto3IamGateway(client_factory=client_factory),
        s3_gateway=Boto3S3Gateway(client_factory=client_factory),
        ec2_gateway=FakeEc2Gateway(),  # unused: no ec2:* resources in these manifests
        vpc_gateway=FakeVpcGateway(),  # unused: no ec2:instance in these manifests
        repository=InMemoryRepository(),
        profile=_PROFILE,
        region=_REGION,
        logger=logging.getLogger("test.stack_moto"),
        default_key_pair_dir=Path("/tmp"),
    )
    return StackEngine(
        steps=build_steps(deps),
        repository=InMemoryStackRepository(),
        logger=logging.getLogger("test.stack_moto"),
        profile=_PROFILE,
        region=_REGION,
    )


def _raw_iam() -> IAMClient:
    return boto3.client("iam", region_name=_REGION)


def _raw_s3() -> S3Client:
    return boto3.client("s3", region_name=_REGION)


def _happy_manifest() -> StackManifest:
    return StackManifest(
        apiVersion="v1",
        name="moto-stack",
        resources=[
            _resource("app-bucket", ResourceKind.S3_BUCKET, {"bucket_name": "moto-stack-bucket"}),
            _resource(
                "app-role",
                ResourceKind.IAM_ROLE,
                {"role_name": "moto-stack-role", "service": "ec2.amazonaws.com"},
            ),
            _resource(
                "bucket-read-policy",
                ResourceKind.IAM_POLICY,
                {
                    "policy_name": "moto-stack-s3-read",
                    "document": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["s3:GetObject"],
                                "Resource": "${app-bucket.arn}/*",
                            }
                        ],
                    },
                },
            ),
            _resource(
                "attach-policy",
                ResourceKind.IAM_POLICY_ATTACHMENT,
                {
                    "policy_arn": "${bucket-read-policy.arn}",
                    "principal_type": "role",
                    "principal_name": "${app-role.name}",
                },
            ),
        ],
    )


@mock_aws
def test_apply_then_destroy_leaves_nothing_behind() -> None:
    engine = _engine()
    manifest = _happy_manifest()
    iam_client = _raw_iam()
    s3_client = _raw_s3()

    state = engine.apply(manifest)
    assert state.status is StackStatus.APPLIED
    assert iam_client.get_role(RoleName="moto-stack-role")
    assert s3_client.head_bucket(Bucket="moto-stack-bucket")

    final_state = engine.destroy("moto-stack")
    assert final_state.status is StackStatus.DESTROYED

    with pytest.raises(ClientError):
        iam_client.get_role(RoleName="moto-stack-role")
    with pytest.raises(ClientError):
        s3_client.head_bucket(Bucket="moto-stack-bucket")


@mock_aws
def test_rollback_on_a_real_failure_cleans_up_earlier_resources() -> None:
    engine = _engine()
    iam_client = _raw_iam()
    s3_client = _raw_s3()
    manifest = StackManifest(
        apiVersion="v1",
        name="moto-rollback-stack",
        resources=[
            _resource(
                "app-bucket", ResourceKind.S3_BUCKET, {"bucket_name": "moto-rollback-bucket"}
            ),
            _resource(
                "app-role",
                ResourceKind.IAM_ROLE,
                {"role_name": "moto-rollback-role", "service": "ec2.amazonaws.com"},
            ),
            _resource(
                "attach-policy",
                ResourceKind.IAM_POLICY_ATTACHMENT,
                {
                    # A syntactically valid but nonexistent policy ARN -- AWS/moto rejects
                    # attaching it, forcing a real failure this late in the manifest.
                    "policy_arn": "arn:aws:iam::123456789012:policy/does-not-exist",
                    "principal_type": "role",
                    "principal_name": "${app-role.name}",
                },
                depends_on=["app-bucket"],
            ),
        ],
    )

    with pytest.raises(StackApplyError) as exc_info:
        engine.apply(manifest)

    assert exc_info.value.orphaned == []  # rollback fully succeeded

    with pytest.raises(ClientError):
        iam_client.get_role(RoleName="moto-rollback-role")
    with pytest.raises(ClientError):
        s3_client.head_bucket(Bucket="moto-rollback-bucket")

"""The step registry: one ``StackStep`` per ``ResourceKind``, and nothing the engine special-cases.

Adding a new resource kind to this project means: add a ``ResourceKind``
member, write one ``StackStep`` adapter under ``steps/``, and add one entry
here. ``StackEngine`` itself never grows an ``if kind == ...`` branch -- same
Open/Closed spirit as ``ClientFactory``'s ``_SERVICE_CONFIG_OVERRIDES``
registry (see ``docs/architecture.md``).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from aws_admin_cli.application.services.ami_resolver import AmiResolver
from aws_admin_cli.application.services.network_resolver import NetworkResolver
from aws_admin_cli.application.stacks.steps.base import StackStep
from aws_admin_cli.application.stacks.steps.ec2_instance_step import Ec2InstanceStep
from aws_admin_cli.application.stacks.steps.ec2_key_pair_step import Ec2KeyPairStep
from aws_admin_cli.application.stacks.steps.iam_instance_profile_step import (
    IamInstanceProfileStep,
)
from aws_admin_cli.application.stacks.steps.iam_policy_attachment_step import (
    IamPolicyAttachmentStep,
)
from aws_admin_cli.application.stacks.steps.iam_policy_step import IamPolicyStep
from aws_admin_cli.application.stacks.steps.iam_role_step import IamRoleStep
from aws_admin_cli.application.stacks.steps.s3_bucket_policy_step import S3BucketPolicyStep
from aws_admin_cli.application.stacks.steps.s3_bucket_step import S3BucketStep
from aws_admin_cli.application.use_cases.ec2.create_key_pair import CreateKeyPairUseCase
from aws_admin_cli.application.use_cases.ec2.delete_key_pair import DeleteKeyPairUseCase
from aws_admin_cli.application.use_cases.ec2.get_instance import GetInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.launch_instance import LaunchInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.list_key_pairs import ListKeyPairsUseCase
from aws_admin_cli.application.use_cases.ec2.terminate_instance import TerminateInstanceUseCase
from aws_admin_cli.application.use_cases.iam.attach_policy import AttachPolicyUseCase
from aws_admin_cli.application.use_cases.iam.create_policy import CreatePolicyUseCase
from aws_admin_cli.application.use_cases.iam.create_role import CreateRoleUseCase
from aws_admin_cli.application.use_cases.iam.delete_instance_profile import (
    DeleteInstanceProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.delete_policy import DeletePolicyUseCase
from aws_admin_cli.application.use_cases.iam.delete_role import DeleteRoleUseCase
from aws_admin_cli.application.use_cases.iam.detach_policy import DetachPolicyUseCase
from aws_admin_cli.application.use_cases.iam.ensure_instance_profile_for_role import (
    EnsureInstanceProfileForRoleUseCase,
)
from aws_admin_cli.application.use_cases.iam.get_instance_profile import GetInstanceProfileUseCase
from aws_admin_cli.application.use_cases.iam.get_role import GetRoleUseCase
from aws_admin_cli.application.use_cases.iam.list_attached_policies import (
    ListAttachedPoliciesUseCase,
)
from aws_admin_cli.application.use_cases.iam.list_policies import ListPoliciesUseCase
from aws_admin_cli.application.use_cases.s3.create_bucket import CreateBucketUseCase
from aws_admin_cli.application.use_cases.s3.delete_bucket import DeleteBucketUseCase
from aws_admin_cli.application.use_cases.s3.delete_bucket_policy import DeleteBucketPolicyUseCase
from aws_admin_cli.application.use_cases.s3.get_bucket_policy import GetBucketPolicyUseCase
from aws_admin_cli.application.use_cases.s3.list_buckets import ListBucketsUseCase
from aws_admin_cli.application.use_cases.s3.set_bucket_policy import SetBucketPolicyUseCase
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.stack import ResourceKind
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway
from aws_admin_cli.domain.ports.iam_gateway import IamGateway
from aws_admin_cli.domain.ports.repository import Repository
from aws_admin_cli.domain.ports.s3_gateway import S3Gateway
from aws_admin_cli.domain.ports.vpc_gateway import VpcGateway

__all__ = ["StepDependencies", "build_steps"]


@dataclass(frozen=True, slots=True)
class StepDependencies:
    """Everything ``build_steps`` needs to wire up every use case a step delegates to.

    One bag of collaborators, built once by the CLI layer
    (``presentation/cli/stack_app.py``) per invocation -- mirrors how
    ``ec2_app.py`` builds its gateways/resolvers/use cases per command,
    just gathered in one place since ``StackEngine`` needs all eight steps
    at once, not one at a time.
    """

    iam_gateway: IamGateway
    s3_gateway: S3Gateway
    ec2_gateway: Ec2Gateway
    vpc_gateway: VpcGateway
    repository: Repository[ResourceRecord]
    profile: str
    region: str
    logger: logging.Logger
    default_key_pair_dir: Path


def build_steps(deps: StepDependencies) -> dict[ResourceKind, StackStep]:
    """Instantiate one ``StackStep`` per ``ResourceKind``, each wired to its use case(s)."""
    network_resolver = NetworkResolver(gateway=deps.vpc_gateway)
    ami_resolver = AmiResolver(gateway=deps.ec2_gateway)
    ensure_instance_profile = EnsureInstanceProfileForRoleUseCase(
        gateway=deps.iam_gateway,
        repository=deps.repository,
        profile=deps.profile,
        region=deps.region,
    )

    steps: Final[dict[ResourceKind, StackStep]] = {
        ResourceKind.IAM_ROLE: IamRoleStep(
            get_use_case=GetRoleUseCase(gateway=deps.iam_gateway),
            create_use_case=CreateRoleUseCase(
                gateway=deps.iam_gateway,
                repository=deps.repository,
                profile=deps.profile,
                region=deps.region,
            ),
            delete_use_case=DeleteRoleUseCase(gateway=deps.iam_gateway, repository=deps.repository),
            get_instance_profile_use_case=GetInstanceProfileUseCase(gateway=deps.iam_gateway),
            delete_instance_profile_use_case=DeleteInstanceProfileUseCase(
                gateway=deps.iam_gateway, repository=deps.repository
            ),
        ),
        ResourceKind.IAM_POLICY: IamPolicyStep(
            list_use_case=ListPoliciesUseCase(gateway=deps.iam_gateway),
            create_use_case=CreatePolicyUseCase(
                gateway=deps.iam_gateway,
                repository=deps.repository,
                profile=deps.profile,
                region=deps.region,
            ),
            delete_use_case=DeletePolicyUseCase(
                gateway=deps.iam_gateway, repository=deps.repository
            ),
        ),
        ResourceKind.IAM_POLICY_ATTACHMENT: IamPolicyAttachmentStep(
            list_use_case=ListAttachedPoliciesUseCase(gateway=deps.iam_gateway),
            attach_use_case=AttachPolicyUseCase(gateway=deps.iam_gateway, logger=deps.logger),
            detach_use_case=DetachPolicyUseCase(gateway=deps.iam_gateway),
        ),
        ResourceKind.IAM_INSTANCE_PROFILE: IamInstanceProfileStep(
            get_use_case=GetInstanceProfileUseCase(gateway=deps.iam_gateway),
            ensure_use_case=ensure_instance_profile,
            delete_use_case=DeleteInstanceProfileUseCase(
                gateway=deps.iam_gateway, repository=deps.repository
            ),
        ),
        ResourceKind.S3_BUCKET: S3BucketStep(
            list_use_case=ListBucketsUseCase(gateway=deps.s3_gateway),
            create_use_case=CreateBucketUseCase(
                gateway=deps.s3_gateway,
                repository=deps.repository,
                profile=deps.profile,
                logger=deps.logger,
            ),
            delete_use_case=DeleteBucketUseCase(
                gateway=deps.s3_gateway, repository=deps.repository
            ),
            default_region=deps.region,
        ),
        ResourceKind.S3_BUCKET_POLICY: S3BucketPolicyStep(
            set_use_case=SetBucketPolicyUseCase(gateway=deps.s3_gateway),
            get_use_case=GetBucketPolicyUseCase(gateway=deps.s3_gateway),
            delete_use_case=DeleteBucketPolicyUseCase(gateway=deps.s3_gateway),
        ),
        ResourceKind.EC2_KEY_PAIR: Ec2KeyPairStep(
            list_use_case=ListKeyPairsUseCase(gateway=deps.ec2_gateway),
            create_use_case=CreateKeyPairUseCase(gateway=deps.ec2_gateway),
            delete_use_case=DeleteKeyPairUseCase(gateway=deps.ec2_gateway),
            default_save_dir=deps.default_key_pair_dir,
        ),
        ResourceKind.EC2_INSTANCE: Ec2InstanceStep(
            launch_use_case=LaunchInstanceUseCase(
                gateway=deps.ec2_gateway,
                ami_resolver=ami_resolver,
                network_resolver=network_resolver,
                ensure_instance_profile=ensure_instance_profile,
                repository=deps.repository,
                profile=deps.profile,
                region=deps.region,
                logger=deps.logger,
            ),
            get_use_case=GetInstanceUseCase(gateway=deps.ec2_gateway),
            terminate_use_case=TerminateInstanceUseCase(
                gateway=deps.ec2_gateway, repository=deps.repository
            ),
        ),
    }
    return steps

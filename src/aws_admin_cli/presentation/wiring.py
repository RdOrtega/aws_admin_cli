"""The single composition point for every use case ``presentation/`` calls.

Before this module, each Typer command in ``presentation/cli/*.py`` built its
own use case(s) inline (``CreateUserUseCase(gateway=Boto3IamGateway(...), ...)``).
That was fine with one caller per use case; it stops being fine the moment a
SECOND caller (the TUI, ``presentation/tui/``) needs the exact same objects --
two independent construction sites for the same use case are two places that
can quietly drift out of sync (a new required constructor argument added to
one, forgotten in the other; a repository wired from a different source).
This module is the only place ``Boto3IamGateway``/``Boto3S3Gateway``/
``Boto3VpcGateway``/``Boto3Ec2Gateway`` and every use case built from one are
ever constructed. ``presentation/cli/*.py`` and ``presentation/tui/`` both
call these functions instead of building anything themselves.

Each ``build_*_use_cases(ctx)`` function returns a frozen dataclass of
already-instantiated use cases -- eager, not ``cached_property``-based,
because eager construction here is already free of I/O: building a
``Boto3IamGateway`` (or any of the sibling gateways) only stores a reference
to ``ctx.client_factory``, exactly like every other use case's constructor
here -- none of them touch the network or resolve credentials. The actual
boto3 client only gets built the first time a gateway method executes
(``ClientFactory``'s own laziness, unchanged by this module). So calling
``build_iam_use_cases(ctx)`` and never calling ``.execute()`` on anything it
returns costs nothing beyond a few cheap Python object allocations -- the
"lazy: no boto3 client until something is actually used" requirement is
satisfied by ``ClientFactory``/the gateways themselves, not by deferring
construction here.

Every ``build_*`` function is called fresh, once per command/flow
invocation (exactly how the old inline construction worked) -- there is no
caching across calls here, so a resolver's internal cache
(``AmiResolver``/``NetworkResolver``) still only spans a single command's
execution, never leaks between two unrelated commands.
"""

from dataclasses import dataclass
from pathlib import Path

from aws_admin_cli.application.services.ami_resolver import AmiResolver
from aws_admin_cli.application.services.network_resolver import NetworkResolver
from aws_admin_cli.application.stacks.engine import StackEngine
from aws_admin_cli.application.stacks.registry import StepDependencies, build_steps
from aws_admin_cli.application.use_cases.cloudwatch.create_cpu_alarm import CreateCpuAlarmUseCase
from aws_admin_cli.application.use_cases.cloudwatch.create_status_check_alarm import (
    CreateStatusCheckAlarmUseCase,
)
from aws_admin_cli.application.use_cases.cloudwatch.get_average_cpu_utilization import (
    GetAverageCpuUtilizationUseCase,
)
from aws_admin_cli.application.use_cases.cloudwatch.list_alarms import ListAlarmsUseCase
from aws_admin_cli.application.use_cases.ec2.copy_ami import CopyAmiUseCase
from aws_admin_cli.application.use_cases.ec2.create_ami import CreateAmiUseCase
from aws_admin_cli.application.use_cases.ec2.create_key_pair import CreateKeyPairUseCase
from aws_admin_cli.application.use_cases.ec2.create_snapshot import CreateSnapshotUseCase
from aws_admin_cli.application.use_cases.ec2.delete_instance_tags import DeleteInstanceTagsUseCase
from aws_admin_cli.application.use_cases.ec2.delete_key_pair import DeleteKeyPairUseCase
from aws_admin_cli.application.use_cases.ec2.deregister_ami import DeregisterAmiUseCase
from aws_admin_cli.application.use_cases.ec2.get_ami import GetAmiUseCase
from aws_admin_cli.application.use_cases.ec2.get_console_output import GetConsoleOutputUseCase
from aws_admin_cli.application.use_cases.ec2.get_instance import GetInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.launch_instance import LaunchInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.list_amis import ListAmisUseCase
from aws_admin_cli.application.use_cases.ec2.list_amis_global import ListAmisGlobalUseCase
from aws_admin_cli.application.use_cases.ec2.list_instances import ListInstancesUseCase
from aws_admin_cli.application.use_cases.ec2.list_instances_global import (
    ListInstancesGlobalUseCase,
)
from aws_admin_cli.application.use_cases.ec2.list_key_pairs import ListKeyPairsUseCase
from aws_admin_cli.application.use_cases.ec2.reboot_instance import RebootInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.set_instance_security_groups import (
    SetInstanceSecurityGroupsUseCase,
)
from aws_admin_cli.application.use_cases.ec2.set_instance_tags import SetInstanceTagsUseCase
from aws_admin_cli.application.use_cases.ec2.start_instance import StartInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.stop_instance import StopInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.tag_resource import TagEc2ResourceUseCase
from aws_admin_cli.application.use_cases.ec2.terminate_instance import TerminateInstanceUseCase
from aws_admin_cli.application.use_cases.ec2.untag_resource import UntagEc2ResourceUseCase
from aws_admin_cli.application.use_cases.iam.add_user_to_group import AddUserToGroupUseCase
from aws_admin_cli.application.use_cases.iam.attach_policy import AttachPolicyUseCase
from aws_admin_cli.application.use_cases.iam.attach_role_to_profile import (
    AttachRoleToProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.copy_user import CopyUserUseCase
from aws_admin_cli.application.use_cases.iam.create_access_key import CreateAccessKeyUseCase
from aws_admin_cli.application.use_cases.iam.create_group import CreateGroupUseCase
from aws_admin_cli.application.use_cases.iam.create_instance_profile import (
    CreateInstanceProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.create_policy import CreatePolicyUseCase
from aws_admin_cli.application.use_cases.iam.create_role import CreateRoleUseCase
from aws_admin_cli.application.use_cases.iam.create_user import CreateUserUseCase
from aws_admin_cli.application.use_cases.iam.deactivate_mfa_device import (
    DeactivateMfaDeviceUseCase,
)
from aws_admin_cli.application.use_cases.iam.delete_access_key import DeleteAccessKeyUseCase
from aws_admin_cli.application.use_cases.iam.delete_group import DeleteGroupUseCase
from aws_admin_cli.application.use_cases.iam.delete_instance_profile import (
    DeleteInstanceProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.delete_login_profile import (
    DeleteLoginProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.delete_policy import DeletePolicyUseCase
from aws_admin_cli.application.use_cases.iam.delete_role import DeleteRoleUseCase
from aws_admin_cli.application.use_cases.iam.delete_user import DeleteUserUseCase
from aws_admin_cli.application.use_cases.iam.delete_user_tags import DeleteUserTagsUseCase
from aws_admin_cli.application.use_cases.iam.detach_policy import DetachPolicyUseCase
from aws_admin_cli.application.use_cases.iam.detach_role_from_profile import (
    DetachRoleFromProfileUseCase,
)
from aws_admin_cli.application.use_cases.iam.ensure_instance_profile_for_role import (
    EnsureInstanceProfileForRoleUseCase,
)
from aws_admin_cli.application.use_cases.iam.get_access_key_last_used import (
    GetAccessKeyLastUsedUseCase,
)
from aws_admin_cli.application.use_cases.iam.get_credential_report import (
    GetCredentialReportUseCase,
)
from aws_admin_cli.application.use_cases.iam.get_instance_profile import GetInstanceProfileUseCase
from aws_admin_cli.application.use_cases.iam.get_policy import GetPolicyUseCase
from aws_admin_cli.application.use_cases.iam.get_policy_document import GetPolicyDocumentUseCase
from aws_admin_cli.application.use_cases.iam.get_role import GetRoleUseCase
from aws_admin_cli.application.use_cases.iam.get_user import GetUserUseCase
from aws_admin_cli.application.use_cases.iam.get_user_detail import GetUserDetailUseCase
from aws_admin_cli.application.use_cases.iam.list_access_keys import ListAccessKeysUseCase
from aws_admin_cli.application.use_cases.iam.list_attached_policies import (
    ListAttachedPoliciesUseCase,
)
from aws_admin_cli.application.use_cases.iam.list_groups import ListGroupsUseCase
from aws_admin_cli.application.use_cases.iam.list_groups_for_user import (
    ListGroupsForUserUseCase,
)
from aws_admin_cli.application.use_cases.iam.list_instance_profiles import (
    ListInstanceProfilesUseCase,
)
from aws_admin_cli.application.use_cases.iam.list_policies import ListPoliciesUseCase
from aws_admin_cli.application.use_cases.iam.list_roles import ListRolesUseCase
from aws_admin_cli.application.use_cases.iam.list_users import ListUsersUseCase
from aws_admin_cli.application.use_cases.iam.remove_user_from_group import (
    RemoveUserFromGroupUseCase,
)
from aws_admin_cli.application.use_cases.iam.resolve_deny_all_policy import (
    ResolveDenyAllPolicyUseCase,
)
from aws_admin_cli.application.use_cases.iam.set_login_profile import SetLoginProfileUseCase
from aws_admin_cli.application.use_cases.iam.set_user_tags import SetUserTagsUseCase
from aws_admin_cli.application.use_cases.iam.update_access_key import UpdateAccessKeyUseCase
from aws_admin_cli.application.use_cases.iam.update_user import UpdateUserUseCase
from aws_admin_cli.application.use_cases.s3.audit_buckets import AuditBucketsUseCase
from aws_admin_cli.application.use_cases.s3.copy_object import CopyObjectUseCase
from aws_admin_cli.application.use_cases.s3.create_bucket import CreateBucketUseCase
from aws_admin_cli.application.use_cases.s3.delete_bucket import DeleteBucketUseCase
from aws_admin_cli.application.use_cases.s3.delete_bucket_policy import DeleteBucketPolicyUseCase
from aws_admin_cli.application.use_cases.s3.delete_object import DeleteObjectUseCase
from aws_admin_cli.application.use_cases.s3.delete_prefix import DeletePrefixUseCase
from aws_admin_cli.application.use_cases.s3.download_object import DownloadObjectUseCase
from aws_admin_cli.application.use_cases.s3.empty_bucket import EmptyBucketUseCase
from aws_admin_cli.application.use_cases.s3.get_bucket_access import GetBucketAccessUseCase
from aws_admin_cli.application.use_cases.s3.get_bucket_info import GetBucketInfoUseCase
from aws_admin_cli.application.use_cases.s3.get_bucket_policy import GetBucketPolicyUseCase
from aws_admin_cli.application.use_cases.s3.get_bucket_tags import GetBucketTagsUseCase
from aws_admin_cli.application.use_cases.s3.list_bucket_access import ListBucketAccessUseCase
from aws_admin_cli.application.use_cases.s3.list_buckets import ListBucketsUseCase
from aws_admin_cli.application.use_cases.s3.list_objects import ListObjectsUseCase
from aws_admin_cli.application.use_cases.s3.presign_url import PresignUrlUseCase
from aws_admin_cli.application.use_cases.s3.set_bucket_policy import SetBucketPolicyUseCase
from aws_admin_cli.application.use_cases.s3.set_bucket_tags import SetBucketTagsUseCase
from aws_admin_cli.application.use_cases.s3.set_public_access import SetBucketPublicAccessUseCase
from aws_admin_cli.application.use_cases.s3.set_versioning import SetVersioningUseCase
from aws_admin_cli.application.use_cases.s3.upload_directory import UploadDirectoryUseCase
from aws_admin_cli.application.use_cases.s3.upload_object import UploadObjectUseCase
from aws_admin_cli.application.use_cases.vpc.audit_security_groups import (
    AuditSecurityGroupsUseCase,
)
from aws_admin_cli.application.use_cases.vpc.get_vpc_details import GetVpcDetailsUseCase
from aws_admin_cli.application.use_cases.vpc.list_availability_zones import (
    ListAvailabilityZonesUseCase,
)
from aws_admin_cli.application.use_cases.vpc.list_security_groups import (
    ListSecurityGroupsUseCase,
)
from aws_admin_cli.application.use_cases.vpc.list_subnets import ListSubnetsUseCase
from aws_admin_cli.application.use_cases.vpc.list_vpcs import ListVpcsUseCase
from aws_admin_cli.core.context import AppContext
from aws_admin_cli.infrastructure.aws.gateways.boto3_cloudwatch_gateway import (
    Boto3CloudWatchGateway,
)
from aws_admin_cli.infrastructure.aws.gateways.boto3_ec2_gateway import Boto3Ec2Gateway
from aws_admin_cli.infrastructure.aws.gateways.boto3_iam_gateway import Boto3IamGateway
from aws_admin_cli.infrastructure.aws.gateways.boto3_s3_gateway import Boto3S3Gateway
from aws_admin_cli.infrastructure.aws.gateways.boto3_vpc_gateway import Boto3VpcGateway
from aws_admin_cli.infrastructure.aws.lambda_service import LambdaService

__all__ = [
    "CloudWatchUseCases",
    "Ec2UseCases",
    "IamUseCases",
    "S3UseCases",
    "VpcUseCases",
    "build_cloudwatch_use_cases",
    "build_ec2_use_cases",
    "build_iam_use_cases",
    "build_lambda_service",
    "build_s3_use_cases",
    "build_stack_engine",
    "build_vpc_use_cases",
]


# == IAM ========================================================================


@dataclass(frozen=True, slots=True)
class IamUseCases:
    """Every IAM use case, already wired to a shared ``Boto3IamGateway``."""

    gateway: Boto3IamGateway
    create_user: CreateUserUseCase
    list_users: ListUsersUseCase
    get_user: GetUserUseCase
    delete_user: DeleteUserUseCase
    update_user: UpdateUserUseCase
    set_user_tags: SetUserTagsUseCase
    delete_user_tags: DeleteUserTagsUseCase
    attach_policy: AttachPolicyUseCase
    detach_policy: DetachPolicyUseCase
    list_attached_policies: ListAttachedPoliciesUseCase
    create_policy: CreatePolicyUseCase
    list_policies: ListPoliciesUseCase
    get_policy: GetPolicyUseCase
    get_policy_document: GetPolicyDocumentUseCase
    delete_policy: DeletePolicyUseCase
    create_role: CreateRoleUseCase
    list_roles: ListRolesUseCase
    get_role: GetRoleUseCase
    delete_role: DeleteRoleUseCase
    create_instance_profile: CreateInstanceProfileUseCase
    list_instance_profiles: ListInstanceProfilesUseCase
    get_instance_profile: GetInstanceProfileUseCase
    delete_instance_profile: DeleteInstanceProfileUseCase
    attach_role_to_profile: AttachRoleToProfileUseCase
    detach_role_from_profile: DetachRoleFromProfileUseCase
    copy_user: CopyUserUseCase
    get_user_detail: GetUserDetailUseCase
    create_group: CreateGroupUseCase
    list_groups: ListGroupsUseCase
    delete_group: DeleteGroupUseCase
    add_user_to_group: AddUserToGroupUseCase
    remove_user_from_group: RemoveUserFromGroupUseCase
    list_groups_for_user: ListGroupsForUserUseCase
    set_login_profile: SetLoginProfileUseCase
    delete_login_profile: DeleteLoginProfileUseCase
    create_access_key: CreateAccessKeyUseCase
    list_access_keys: ListAccessKeysUseCase
    update_access_key: UpdateAccessKeyUseCase
    delete_access_key: DeleteAccessKeyUseCase
    deactivate_mfa_device: DeactivateMfaDeviceUseCase
    get_access_key_last_used: GetAccessKeyLastUsedUseCase
    resolve_deny_all_policy: ResolveDenyAllPolicyUseCase
    get_credential_report: GetCredentialReportUseCase


def build_iam_use_cases(ctx: AppContext) -> IamUseCases:
    """Wire every IAM use case for this invocation."""
    gateway = Boto3IamGateway(client_factory=ctx.client_factory)
    repository = ctx.resource_repository
    profile = ctx.settings.profile
    region = ctx.settings.region
    create_user = CreateUserUseCase(
        gateway=gateway, repository=repository, profile=profile, region=region
    )
    create_policy = CreatePolicyUseCase(
        gateway=gateway, repository=repository, profile=profile, region=region
    )
    return IamUseCases(
        gateway=gateway,
        create_user=create_user,
        list_users=ListUsersUseCase(gateway=gateway),
        get_user=GetUserUseCase(gateway=gateway),
        delete_user=DeleteUserUseCase(gateway=gateway, repository=repository),
        update_user=UpdateUserUseCase(gateway=gateway),
        set_user_tags=SetUserTagsUseCase(gateway=gateway),
        delete_user_tags=DeleteUserTagsUseCase(gateway=gateway),
        attach_policy=AttachPolicyUseCase(gateway=gateway, logger=ctx.logger),
        detach_policy=DetachPolicyUseCase(gateway=gateway),
        list_attached_policies=ListAttachedPoliciesUseCase(gateway=gateway),
        create_policy=create_policy,
        list_policies=ListPoliciesUseCase(gateway=gateway),
        get_policy=GetPolicyUseCase(gateway=gateway),
        get_policy_document=GetPolicyDocumentUseCase(gateway=gateway),
        delete_policy=DeletePolicyUseCase(gateway=gateway, repository=repository),
        create_role=CreateRoleUseCase(
            gateway=gateway, repository=repository, profile=profile, region=region
        ),
        list_roles=ListRolesUseCase(gateway=gateway),
        get_role=GetRoleUseCase(gateway=gateway),
        delete_role=DeleteRoleUseCase(gateway=gateway, repository=repository),
        create_instance_profile=CreateInstanceProfileUseCase(
            gateway=gateway, repository=repository, profile=profile, region=region
        ),
        list_instance_profiles=ListInstanceProfilesUseCase(gateway=gateway),
        get_instance_profile=GetInstanceProfileUseCase(gateway=gateway),
        delete_instance_profile=DeleteInstanceProfileUseCase(
            gateway=gateway, repository=repository
        ),
        attach_role_to_profile=AttachRoleToProfileUseCase(gateway=gateway),
        detach_role_from_profile=DetachRoleFromProfileUseCase(gateway=gateway),
        copy_user=CopyUserUseCase(gateway=gateway, create_user=create_user),
        get_user_detail=GetUserDetailUseCase(gateway=gateway),
        create_group=CreateGroupUseCase(gateway=gateway),
        list_groups=ListGroupsUseCase(gateway=gateway),
        delete_group=DeleteGroupUseCase(gateway=gateway),
        add_user_to_group=AddUserToGroupUseCase(gateway=gateway),
        remove_user_from_group=RemoveUserFromGroupUseCase(gateway=gateway),
        list_groups_for_user=ListGroupsForUserUseCase(gateway=gateway),
        set_login_profile=SetLoginProfileUseCase(gateway=gateway),
        delete_login_profile=DeleteLoginProfileUseCase(gateway=gateway),
        create_access_key=CreateAccessKeyUseCase(gateway=gateway),
        list_access_keys=ListAccessKeysUseCase(gateway=gateway),
        update_access_key=UpdateAccessKeyUseCase(gateway=gateway),
        delete_access_key=DeleteAccessKeyUseCase(gateway=gateway),
        deactivate_mfa_device=DeactivateMfaDeviceUseCase(gateway=gateway),
        get_access_key_last_used=GetAccessKeyLastUsedUseCase(gateway=gateway),
        resolve_deny_all_policy=ResolveDenyAllPolicyUseCase(
            gateway=gateway, create_policy=create_policy
        ),
        get_credential_report=GetCredentialReportUseCase(gateway=gateway),
    )


# == S3 =========================================================================


@dataclass(frozen=True, slots=True)
class S3UseCases:
    """Every S3 use case, already wired to a shared ``Boto3S3Gateway``."""

    gateway: Boto3S3Gateway
    create_bucket: CreateBucketUseCase
    list_buckets: ListBucketsUseCase
    get_bucket_info: GetBucketInfoUseCase
    get_bucket_access: GetBucketAccessUseCase
    list_bucket_access: ListBucketAccessUseCase
    delete_bucket: DeleteBucketUseCase
    set_versioning: SetVersioningUseCase
    set_public_access: SetBucketPublicAccessUseCase
    set_bucket_tags: SetBucketTagsUseCase
    get_bucket_tags: GetBucketTagsUseCase
    get_bucket_policy: GetBucketPolicyUseCase
    set_bucket_policy: SetBucketPolicyUseCase
    delete_bucket_policy: DeleteBucketPolicyUseCase
    list_objects: ListObjectsUseCase
    copy_object: CopyObjectUseCase
    upload_object: UploadObjectUseCase
    upload_directory: UploadDirectoryUseCase
    download_object: DownloadObjectUseCase
    delete_object: DeleteObjectUseCase
    delete_prefix: DeletePrefixUseCase
    presign_url: PresignUrlUseCase
    empty_bucket: EmptyBucketUseCase
    audit_buckets: AuditBucketsUseCase


def build_s3_use_cases(ctx: AppContext) -> S3UseCases:
    """Wire every S3 use case for this invocation."""
    gateway = Boto3S3Gateway(client_factory=ctx.client_factory)
    repository = ctx.resource_repository
    profile = ctx.settings.profile
    region = ctx.settings.region
    upload_object = UploadObjectUseCase(
        gateway=gateway, repository=repository, profile=profile, region=region
    )
    get_bucket_access = GetBucketAccessUseCase(gateway=gateway)
    return S3UseCases(
        gateway=gateway,
        create_bucket=CreateBucketUseCase(
            gateway=gateway, repository=repository, profile=profile, logger=ctx.logger
        ),
        list_buckets=ListBucketsUseCase(gateway=gateway),
        get_bucket_info=GetBucketInfoUseCase(gateway=gateway),
        get_bucket_access=get_bucket_access,
        list_bucket_access=ListBucketAccessUseCase(get_bucket_access=get_bucket_access),
        delete_bucket=DeleteBucketUseCase(gateway=gateway, repository=repository),
        set_versioning=SetVersioningUseCase(gateway=gateway),
        set_public_access=SetBucketPublicAccessUseCase(gateway=gateway),
        set_bucket_tags=SetBucketTagsUseCase(gateway=gateway),
        get_bucket_tags=GetBucketTagsUseCase(gateway=gateway),
        get_bucket_policy=GetBucketPolicyUseCase(gateway=gateway),
        set_bucket_policy=SetBucketPolicyUseCase(gateway=gateway),
        delete_bucket_policy=DeleteBucketPolicyUseCase(gateway=gateway),
        list_objects=ListObjectsUseCase(gateway=gateway),
        copy_object=CopyObjectUseCase(gateway=gateway),
        upload_object=upload_object,
        upload_directory=UploadDirectoryUseCase(upload_object=upload_object),
        download_object=DownloadObjectUseCase(gateway=gateway),
        delete_object=DeleteObjectUseCase(gateway=gateway, repository=repository),
        delete_prefix=DeletePrefixUseCase(gateway=gateway, repository=repository),
        presign_url=PresignUrlUseCase(gateway=gateway, logger=ctx.logger),
        empty_bucket=EmptyBucketUseCase(gateway=gateway),
        audit_buckets=AuditBucketsUseCase(gateway=gateway),
    )


# == VPC (read-only) ============================================================


@dataclass(frozen=True, slots=True)
class VpcUseCases:
    """Every VPC use case, already wired to a shared ``Boto3VpcGateway``.

    Every one of these is a ``describe_*``/audit/resolve operation -- see
    ``domain/ports/vpc_gateway.py`` and ``docs/least-privilege.md``'s
    "Separation of Duties" section for why this dataclass, deliberately, has
    no create/update/delete use case to wire.
    """

    gateway: Boto3VpcGateway
    resolver: NetworkResolver
    list_vpcs: ListVpcsUseCase
    get_vpc_details: GetVpcDetailsUseCase
    list_subnets: ListSubnetsUseCase
    list_security_groups: ListSecurityGroupsUseCase
    audit_security_groups: AuditSecurityGroupsUseCase
    list_availability_zones: ListAvailabilityZonesUseCase


def build_vpc_use_cases(ctx: AppContext) -> VpcUseCases:
    """Wire every VPC use case for this invocation."""
    gateway = Boto3VpcGateway(client_factory=ctx.client_factory)
    return VpcUseCases(
        gateway=gateway,
        resolver=NetworkResolver(gateway=gateway),
        list_vpcs=ListVpcsUseCase(gateway=gateway),
        get_vpc_details=GetVpcDetailsUseCase(gateway=gateway),
        list_subnets=ListSubnetsUseCase(gateway=gateway, logger=ctx.logger),
        list_security_groups=ListSecurityGroupsUseCase(gateway=gateway),
        audit_security_groups=AuditSecurityGroupsUseCase(gateway=gateway),
        list_availability_zones=ListAvailabilityZonesUseCase(gateway=gateway),
    )


# == EC2 ========================================================================


@dataclass(frozen=True, slots=True)
class Ec2UseCases:
    """Every EC2 use case, already wired to a shared ``Boto3Ec2Gateway``.

    ``ami_resolver``/``network_resolver`` are exposed directly (not just
    baked into ``launch_instance``) because ``ec2 instance launch`` -- and
    the TUI's equivalent flow -- needs to resolve the same AMI/subnet/SGs
    itself first, to show the pre-launch summary, before calling
    ``launch_instance``. Both places use these SAME resolver instances, so
    resolving twice (once for the summary, once inside the use case) costs
    one API call, not two -- the resolver's own cache absorbs the repeat.
    """

    gateway: Boto3Ec2Gateway
    vpc_gateway: Boto3VpcGateway
    iam_gateway: Boto3IamGateway
    network_resolver: NetworkResolver
    ami_resolver: AmiResolver
    create_key_pair: CreateKeyPairUseCase
    delete_key_pair: DeleteKeyPairUseCase
    list_key_pairs: ListKeyPairsUseCase
    list_amis: ListAmisUseCase
    list_amis_global: ListAmisGlobalUseCase
    get_ami: GetAmiUseCase
    get_instance: GetInstanceUseCase
    list_instances: ListInstancesUseCase
    list_instances_global: ListInstancesGlobalUseCase
    start_instance: StartInstanceUseCase
    stop_instance: StopInstanceUseCase
    reboot_instance: RebootInstanceUseCase
    terminate_instance: TerminateInstanceUseCase
    get_console_output: GetConsoleOutputUseCase
    launch_instance: LaunchInstanceUseCase
    set_instance_tags: SetInstanceTagsUseCase
    delete_instance_tags: DeleteInstanceTagsUseCase
    tag_resource: TagEc2ResourceUseCase
    untag_resource: UntagEc2ResourceUseCase
    set_instance_security_groups: SetInstanceSecurityGroupsUseCase
    create_ami: CreateAmiUseCase
    copy_ami: CopyAmiUseCase
    deregister_ami: DeregisterAmiUseCase
    create_snapshot: CreateSnapshotUseCase


def build_ec2_use_cases(ctx: AppContext) -> Ec2UseCases:
    """Wire every EC2 use case for this invocation."""
    gateway = Boto3Ec2Gateway(client_factory=ctx.client_factory)
    vpc_gateway = Boto3VpcGateway(client_factory=ctx.client_factory)
    iam_gateway = Boto3IamGateway(client_factory=ctx.client_factory)
    network_resolver = NetworkResolver(gateway=vpc_gateway)
    ami_resolver = AmiResolver(gateway=gateway)
    repository = ctx.resource_repository
    profile = ctx.settings.profile
    region = ctx.settings.region
    ensure_instance_profile = EnsureInstanceProfileForRoleUseCase(
        gateway=iam_gateway, repository=repository, profile=profile, region=region
    )
    return Ec2UseCases(
        gateway=gateway,
        vpc_gateway=vpc_gateway,
        iam_gateway=iam_gateway,
        network_resolver=network_resolver,
        ami_resolver=ami_resolver,
        create_key_pair=CreateKeyPairUseCase(gateway=gateway),
        delete_key_pair=DeleteKeyPairUseCase(gateway=gateway),
        list_key_pairs=ListKeyPairsUseCase(gateway=gateway),
        list_amis=ListAmisUseCase(gateway=gateway),
        list_amis_global=ListAmisGlobalUseCase(gateway=gateway),
        get_ami=GetAmiUseCase(gateway=gateway),
        get_instance=GetInstanceUseCase(gateway=gateway),
        list_instances=ListInstancesUseCase(gateway=gateway, network_resolver=network_resolver),
        list_instances_global=ListInstancesGlobalUseCase(
            gateway=gateway, network_resolver=network_resolver
        ),
        start_instance=StartInstanceUseCase(gateway=gateway),
        stop_instance=StopInstanceUseCase(gateway=gateway),
        reboot_instance=RebootInstanceUseCase(gateway=gateway),
        terminate_instance=TerminateInstanceUseCase(gateway=gateway, repository=repository),
        get_console_output=GetConsoleOutputUseCase(gateway=gateway),
        launch_instance=LaunchInstanceUseCase(
            gateway=gateway,
            ami_resolver=ami_resolver,
            network_resolver=network_resolver,
            ensure_instance_profile=ensure_instance_profile,
            repository=repository,
            profile=profile,
            region=region,
            logger=ctx.logger,
        ),
        set_instance_tags=SetInstanceTagsUseCase(gateway=gateway),
        delete_instance_tags=DeleteInstanceTagsUseCase(gateway=gateway),
        tag_resource=TagEc2ResourceUseCase(gateway=gateway),
        untag_resource=UntagEc2ResourceUseCase(gateway=gateway),
        set_instance_security_groups=SetInstanceSecurityGroupsUseCase(gateway=gateway),
        create_ami=CreateAmiUseCase(gateway=gateway),
        copy_ami=CopyAmiUseCase(gateway=gateway, source_region=region),
        deregister_ami=DeregisterAmiUseCase(gateway=gateway),
        create_snapshot=CreateSnapshotUseCase(gateway=gateway),
    )


# == CloudWatch =================================================================


@dataclass(frozen=True, slots=True)
class CloudWatchUseCases:
    """Every CloudWatch use case, already wired to a shared ``Boto3CloudWatchGateway``."""

    gateway: Boto3CloudWatchGateway
    list_alarms: ListAlarmsUseCase
    create_cpu_alarm: CreateCpuAlarmUseCase
    create_status_check_alarm: CreateStatusCheckAlarmUseCase
    get_average_cpu_utilization: GetAverageCpuUtilizationUseCase


def build_cloudwatch_use_cases(ctx: AppContext) -> CloudWatchUseCases:
    """Wire every CloudWatch use case for this invocation."""
    gateway = Boto3CloudWatchGateway(client_factory=ctx.client_factory)
    return CloudWatchUseCases(
        gateway=gateway,
        list_alarms=ListAlarmsUseCase(gateway=gateway),
        create_cpu_alarm=CreateCpuAlarmUseCase(gateway=gateway),
        create_status_check_alarm=CreateStatusCheckAlarmUseCase(gateway=gateway),
        get_average_cpu_utilization=GetAverageCpuUtilizationUseCase(gateway=gateway),
    )


# == Lambda ======================================================================


def build_lambda_service(ctx: AppContext) -> LambdaService:
    """Wire the (basic-scaffolding) Lambda service for this invocation.

    A single service, not a ``*UseCases`` bundle -- see ``lambda_service.py``'s
    own module docstring for why this scaffolding stays outside the usual
    Gateway/UseCase layering for now.
    """
    return LambdaService(client_factory=ctx.client_factory)


# == Stack ======================================================================


def build_stack_engine(ctx: AppContext) -> StackEngine:
    """Wire a ``StackEngine`` (every step, each delegating to the use cases above)."""
    deps = StepDependencies(
        iam_gateway=Boto3IamGateway(client_factory=ctx.client_factory),
        s3_gateway=Boto3S3Gateway(client_factory=ctx.client_factory),
        ec2_gateway=Boto3Ec2Gateway(client_factory=ctx.client_factory),
        vpc_gateway=Boto3VpcGateway(client_factory=ctx.client_factory),
        repository=ctx.resource_repository,
        profile=ctx.settings.profile,
        region=ctx.settings.region,
        logger=ctx.logger,
        default_key_pair_dir=Path.home() / ".ssh",
    )
    return StackEngine(
        steps=build_steps(deps),
        repository=ctx.stack_repository,
        logger=ctx.logger,
        profile=ctx.settings.profile,
        region=ctx.settings.region,
    )

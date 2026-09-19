"""Use case: launch an EC2 instance -- the most important use case in this project.

Orchestrates every collaborator introduced across Fases 3-5: ``AmiResolver``
and ``NetworkResolver`` (read-only) resolve human input to IDs,
``EnsureInstanceProfileForRoleUseCase`` (Fase 5's IAM extension) wraps an
optional role in an instance profile, ``domain.models.ec2.validate_user_data``
and ``domain.policies.launch_rules`` gate the request, and finally
``Ec2Gateway.run_instance`` does the actual (idempotent, tag-at-launch,
IMDSv2-enforced) API call.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

from aws_admin_cli.application.dto.ec2 import LaunchInstanceRequest
from aws_admin_cli.application.services.ami_resolver import AmiResolver
from aws_admin_cli.application.services.network_resolver import NetworkResolver
from aws_admin_cli.application.use_cases.iam.ensure_instance_profile_for_role import (
    EnsureInstanceProfileForRoleUseCase,
)
from aws_admin_cli.core.exceptions import ValidationError
from aws_admin_cli.domain.models.common import ResourceRecord
from aws_admin_cli.domain.models.ec2 import Instance, InstanceState, LaunchSpec, validate_user_data
from aws_admin_cli.domain.models.vpc import SecurityGroup, Subnet
from aws_admin_cli.domain.policies.launch_rules import (
    DEFAULT_ALLOWED_FAMILIES,
    check_instance_type,
    check_public_ip,
)
from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway
from aws_admin_cli.domain.ports.repository import Repository

_DEFAULT_POLL_SECONDS = 5
_MANAGED_BY_TAG_KEY = "ManagedBy"
_MANAGED_BY_TAG_VALUE = "aws-admin-cli"


def _compute_client_token(spec: LaunchSpec, nonce: str) -> str:
    """Derive a deterministic ``ClientToken`` from the launch parameters plus a nonce.

    AWS deduplicates ``RunInstances`` calls that share a ``ClientToken``
    within a ~10-minute window: a retried request with the same token returns
    the SAME instance instead of creating a second one. Hashing the actual
    launch parameters (rather than generating a random UUID per call) means
    running the exact same ``ec2 instance launch`` command twice -- with no
    ``--client-token`` at all -- is naturally idempotent, because it produces
    the same token both times. ``nonce`` (``--client-token``, when passed) is
    folded into the hash rather than used as the token directly, so a caller
    who *does* want a second, distinct instance from otherwise-identical
    parameters can force that by changing the nonce.

    ``spec.tags`` always carries a ``CreatedAt`` timestamp this use case
    stamps fresh on every ``execute()`` call (see ``_validate_and_launch``) --
    hashing it verbatim would make the token different on every invocation,
    silently defeating the whole point of this function. It's stripped from
    the hashed tags below; every other tag (including the mandatory
    ``ManagedBy``/``Name`` and any user-supplied ``--tag``) is stable across
    identical invocations and stays in the hash.
    """
    hashed_tags = {key: value for key, value in spec.tags.items() if key != "CreatedAt"}
    canonical = json.dumps(
        {
            "image_id": spec.image_id,
            "instance_type": spec.instance_type,
            "subnet_id": spec.subnet_id,
            "security_group_ids": sorted(spec.security_group_ids),
            "key_name": spec.key_name,
            "iam_instance_profile_arn": spec.iam_instance_profile_arn,
            "user_data": spec.user_data,
            "assign_public_ip": spec.assign_public_ip,
            "volume_size_gb": spec.volume_size_gb,
            "volume_type": spec.volume_type,
            "tags": dict(sorted(hashed_tags.items())),
            "nonce": nonce,
        },
        sort_keys=True,
    )
    # sha256's 64-hex-char digest fits exactly within AWS's 64-character ClientToken limit.
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LaunchInstanceUseCase:
    """Resolve, validate, and launch an EC2 instance."""

    gateway: Ec2Gateway
    ami_resolver: AmiResolver
    network_resolver: NetworkResolver
    ensure_instance_profile: EnsureInstanceProfileForRoleUseCase
    repository: Repository[ResourceRecord]
    profile: str
    region: str
    logger: logging.Logger
    allowed_families: frozenset[str] = DEFAULT_ALLOWED_FAMILIES

    def execute(self: Self, request: LaunchInstanceRequest) -> Instance:
        """Launch the instance ``request`` describes.

        Raises:
            ResourceNotFoundError: The AMI, subnet, security group, or IAM
                role couldn't be resolved.
            ValidationError: A guard rail rejected the request (disallowed
                instance-type family without ``--confirm-large``, a public IP
                without ``--confirm-public`` or into a private subnet, the
                user-data failed size/secret validation, or both
                ``iam_role`` and ``iam_instance_profile_arn`` were given).
        """
        if request.iam_role and request.iam_instance_profile_arn:
            raise ValidationError(
                "No se puede especificar 'iam_role' e 'iam_instance_profile_arn' a la vez.",
                hint="'iam_role' garantiza (crea si falta) un instance profile 1:1 para ese "
                "rol; 'iam_instance_profile_arn' adjunta un profile ya existente "
                "directamente. Usa solo una de las dos.",
            )
        ami = self.ami_resolver.resolve(request.ami_ref)
        vpc = (
            self.network_resolver.resolve_vpc(request.vpc_ref)
            if request.vpc_ref is not None
            else None
        )
        subnet = self.network_resolver.resolve_subnet(request.subnet_ref, vpc=vpc)
        security_groups = self.network_resolver.resolve_security_groups(
            list(request.security_group_refs), vpc=vpc
        )

        # A pre-resolved ARN (e.g. from a stack's own iam:instance-profile resource,
        # via ``${my-profile.arn}``) wins outright: it names a profile that already
        # exists, so there's nothing to ensure. ``iam_role`` is the CLI's own
        # convenience path -- ensure_instance_profile_for_role's 1:1 role<->profile
        # convention -- and only runs when no ARN was given directly.
        iam_instance_profile_arn: str | None = request.iam_instance_profile_arn
        if iam_instance_profile_arn is None and request.iam_role:
            iam_instance_profile_arn = self.ensure_instance_profile.execute(request.iam_role)

        try:
            return self._validate_and_launch(
                request, ami.image_id, subnet, security_groups, iam_instance_profile_arn
            )
        except Exception:
            if iam_instance_profile_arn is not None:
                # The instance profile (idempotent, harmless to leave around) was
                # already created/ensured above -- this failure happened AFTER that,
                # so no rollback here (that's a Fase 6 concern), but the user needs
                # to know it exists.
                self.logger.warning(
                    "El lanzamiento falló, pero el instance profile '%s' (rol '%s') ya "
                    "estaba garantizado antes del fallo y sigue existiendo.",
                    request.iam_role,
                    request.iam_role,
                )
            raise

    def _validate_and_launch(
        self: Self,
        request: LaunchInstanceRequest,
        image_id: str,
        subnet: Subnet,
        security_groups: list[SecurityGroup],
        iam_instance_profile_arn: str | None,
    ) -> Instance:
        user_data = validate_user_data(request.user_data) if request.user_data else None

        check_instance_type(
            request.instance_type, self.allowed_families, confirmed=request.confirm_large
        )
        if request.assign_public_ip and subnet.is_public is None:
            self.logger.warning(
                "No se pudo determinar si la subnet %s es pública o privada; "
                "se procede con --public-ip bajo esa incertidumbre.",
                subnet.subnet_id,
            )
        check_public_ip(
            request.assign_public_ip, subnet.is_public, confirmed=request.confirm_public
        )

        tags = dict(request.tags)
        tags[_MANAGED_BY_TAG_KEY] = _MANAGED_BY_TAG_VALUE
        tags["CreatedAt"] = datetime.now(UTC).isoformat()
        tags["Name"] = request.name

        spec = LaunchSpec(
            image_id=image_id,
            instance_type=request.instance_type,
            subnet_id=subnet.subnet_id,
            security_group_ids=[sg.group_id for sg in security_groups],
            key_name=request.key_name,
            iam_instance_profile_arn=iam_instance_profile_arn,
            user_data=user_data,
            assign_public_ip=request.assign_public_ip,
            volume_size_gb=request.volume_size_gb,
            volume_type=request.volume_type,
            encrypted=True,
            tags=tags,
            min_count=1,
            max_count=1,
        )

        if request.dry_run:
            return self._launch_one(spec, request, index=0)

        instance = self._launch_one(spec, request, index=0)
        for index in range(1, request.count):
            instance = self._launch_one(spec, request, index=index)

        if request.wait:
            self.gateway.wait_for_state(
                [instance.instance_id],
                InstanceState.RUNNING,
                timeout_s=request.timeout_s,
                poll_s=_DEFAULT_POLL_SECONDS,
            )
            instance = self.gateway.get_instance(instance.instance_id)

        self._track(instance, spec)
        return instance

    def _launch_one(
        self: Self, spec: LaunchSpec, request: LaunchInstanceRequest, *, index: int
    ) -> Instance:
        # count > 1 launches N independent instances (N separate run_instance calls,
        # each min/max_count=1) rather than one multi-instance reservation: the
        # Ec2Gateway.run_instance() contract returns a single Instance, so a batch
        # reservation's other members would have nowhere to go without changing that
        # contract. Each gets its own deterministic-but-distinct client token (the
        # loop index folded into the nonce) so they don't collide with each other
        # under AWS's own ClientToken deduplication.
        nonce = f"{request.client_token_nonce or ''}#{index}"
        client_token = _compute_client_token(spec, nonce)
        return self.gateway.run_instance(spec, client_token=client_token, dry_run=request.dry_run)

    def _track(self: Self, instance: Instance, spec: LaunchSpec) -> None:
        self.repository.save(
            ResourceRecord(
                resource_type="ec2:instance",
                identifier=instance.instance_id,
                arn=None,
                profile=self.profile,
                region=self.region,
                created_at=datetime.now(UTC),
                metadata={
                    "ami": spec.image_id,
                    "instance_type": spec.instance_type,
                    "subnet_id": spec.subnet_id,
                    "security_group_ids": spec.security_group_ids,
                    "iam_instance_profile_arn": spec.iam_instance_profile_arn,
                    "name": instance.display_name,
                },
            )
        )

"""The boto3-backed ``Ec2Gateway`` implementation.

Every method is wrapped in :func:`aws_error_boundary`, so nothing from
botocore ever crosses back into ``application/``. Listing operations are
fully paginated except ``describe_key_pairs`` (AWS doesn't paginate it at
all). This module touches ONLY instances/AMIs/key-pairs -- it never calls a
VPC/subnet/security-group mutating operation; see
``domain/ports/ec2_gateway.py``'s module docstring and
``tests/unit/architecture/test_vpc_readonly.py``.
"""

import base64
import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self, cast

from botocore.exceptions import ClientError, WaiterError

from aws_admin_cli.core.exceptions import AwsError, OperationTimeoutError
from aws_admin_cli.domain.models.ec2 import (
    Ami,
    Instance,
    InstanceState,
    KeyMaterial,
    KeyPairInfo,
    LaunchSpec,
)
from aws_admin_cli.infrastructure.aws.client_factory import ClientFactory
from aws_admin_cli.infrastructure.aws.error_mapper import aws_error_boundary

if TYPE_CHECKING:
    from aws_admin_cli.domain.ports.ec2_gateway import Ec2Gateway

_logger = logging.getLogger("aws_admin_cli")

_MANAGED_BY_TAG_KEY = "ManagedBy"
_MANAGED_BY_TAG_VALUE = "aws-admin-cli"
_DEFAULT_ROOT_DEVICE_NAME = "/dev/xvda"
_DRY_RUN_INSTANCE_ID = "dry-run"

_WAITER_NAMES: dict[InstanceState, str] = {
    InstanceState.RUNNING: "instance_running",
    InstanceState.STOPPED: "instance_stopped",
    InstanceState.TERMINATED: "instance_terminated",
}


def _to_filters(mapping: Mapping[str, Sequence[str]] | None) -> list[dict[str, Any]]:
    if not mapping:
        return []
    return [{"Name": name, "Values": list(values)} for name, values in mapping.items()]


def _build_network_interface(spec: LaunchSpec) -> dict[str, Any]:
    """Build the single ``NetworkInterfaces[0]`` entry for ``run_instances``.

    Subnet and security groups MUST travel inside ``NetworkInterfaces`` (with
    ``AssociatePublicIpAddress`` alongside them) rather than as top-level
    ``SubnetId``/``SecurityGroupIds`` parameters -- mixing the two forms is
    rejected by EC2 with ``InvalidParameterCombination``. Isolated as its own
    pure function (no ``self``, no network) so it's testable without a client
    or moto.
    """
    return {
        "DeviceIndex": 0,
        "SubnetId": spec.subnet_id,
        "Groups": list(spec.security_group_ids),
        "AssociatePublicIpAddress": spec.assign_public_ip,
    }


def _flatten_reservations(reservations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten ``DescribeInstances``' ``Reservations[].Instances[]`` nesting into one list."""
    return [
        instance for reservation in reservations for instance in reservation.get("Instances", [])
    ]


@dataclass(frozen=True, slots=True)
class Boto3Ec2Gateway:
    """``Ec2Gateway`` implemented against a real (or LocalStack) EC2 client."""

    client_factory: ClientFactory

    def _client(self: Self, *, region: str | None = None) -> Any:
        return self.client_factory.ec2(region=region)

    # -- AMIs ---------------------------------------------------------------------

    def describe_images(
        self: Self,
        image_ids: Sequence[str] | None,
        owners: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Ami]:
        """List AMIs, fully paginated.

        When ``region`` is given, every returned ``Ami.region`` is stamped
        with it -- same convention as ``describe_instances``.
        """
        kwargs: dict[str, Any] = {}
        if image_ids:
            kwargs["ImageIds"] = list(image_ids)
        if owners:
            kwargs["Owners"] = list(owners)
        if filters:
            kwargs["Filters"] = _to_filters(filters)

        images: list[Ami] = []
        with aws_error_boundary("ec2", "DescribeImages"):
            paginator = self._client(region=region).get_paginator("describe_images")
            for page in paginator.paginate(**kwargs):
                for raw in page.get("Images", []):
                    image = Ami.model_validate(raw)
                    if region is not None:
                        image = image.model_copy(update={"region": region})
                    images.append(image)
        return images

    def _describe_images_in_region(
        self: Self,
        region: str,
        owners: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
    ) -> list[Ami]:
        kwargs: dict[str, Any] = {}
        if owners:
            kwargs["Owners"] = list(owners)
        if filters:
            kwargs["Filters"] = _to_filters(filters)

        client = self.client_factory.ec2(region=region)
        images: list[Ami] = []
        with aws_error_boundary("ec2", "DescribeImages"):
            paginator = client.get_paginator("describe_images")
            for page in paginator.paginate(**kwargs):
                images.extend(
                    Ami.model_validate(raw).model_copy(update={"region": region})
                    for raw in page.get("Images", [])
                )
        return images

    def describe_images_all_regions(
        self: Self,
        owners: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
    ) -> list[Ami]:
        """List AMIs matching ``owners``/``filters`` across every region, fetched in parallel.

        Same per-region failure isolation as ``describe_instances_all_regions``:
        a region this account can't call ``DescribeImages`` against is
        skipped with a logged warning rather than failing the whole scan.
        """
        regions = self._list_regions()
        images: list[Ami] = []
        with ThreadPoolExecutor(max_workers=len(regions) or 1) as executor:
            future_to_region = {
                executor.submit(self._describe_images_in_region, region, owners, filters): region
                for region in regions
            }
            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    images.extend(future.result())
                except AwsError as exc:
                    _logger.warning(
                        "Skipping region '%s' (DescribeImages failed): %s", region, exc
                    )
        return images

    def _tag_specifications(
        self: Self, resource_type: str, tags: Mapping[str, str] | None
    ) -> list[dict[str, Any]]:
        """Build a ``TagSpecifications`` entry for ``resource_type``, or ``[]`` if untagged."""
        if not tags:
            return []
        return [
            {
                "ResourceType": resource_type,
                "Tags": [{"Key": key, "Value": value} for key, value in tags.items()],
            }
        ]

    def create_image(
        self: Self,
        instance_id: str,
        name: str,
        description: str | None,
        *,
        no_reboot: bool,
        tags: Mapping[str, str] | None = None,
        region: str | None = None,
    ) -> Ami:
        """Create an AMI from ``instance_id``, tagged at creation via ``TagSpecifications``."""
        kwargs: dict[str, Any] = {
            "InstanceId": instance_id,
            "Name": name,
            "NoReboot": no_reboot,
            "TagSpecifications": self._tag_specifications("image", tags),
        }
        if description:
            kwargs["Description"] = description
        client = self._client(region=region)
        with aws_error_boundary("ec2", "CreateImage"):
            response = client.create_image(**kwargs)
            image_id = response["ImageId"]
            described = client.describe_images(ImageIds=[image_id])
        return Ami.model_validate(described["Images"][0])

    def copy_image(
        self: Self,
        source_image_id: str,
        *,
        source_region: str,
        name: str,
        description: str | None,
        target_region: str,
        kms_key_id: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> Ami:
        """Copy ``source_image_id`` into ``target_region`` -- the request is sent THERE.

        ``CopyImage``'s destination is whichever region's endpoint the
        request is sent to (``SourceRegion`` only names where it copies
        FROM), so this uses a client pinned to ``target_region`` rather than
        the factory's default profile-region client.
        """
        target_client = self.client_factory.ec2(region=target_region)
        kwargs: dict[str, Any] = {
            "SourceRegion": source_region,
            "SourceImageId": source_image_id,
            "Name": name,
            "TagSpecifications": self._tag_specifications("image", tags),
        }
        if description:
            kwargs["Description"] = description
        if kms_key_id:
            kwargs["Encrypted"] = True
            kwargs["KmsKeyId"] = kms_key_id
        with aws_error_boundary("ec2", "CopyImage"):
            response = target_client.copy_image(**kwargs)
            image_id = response["ImageId"]
            described = target_client.describe_images(ImageIds=[image_id])
        return Ami.model_validate(described["Images"][0])

    def _resolve_root_device_name(self: Self, image_id: str) -> str:
        """Look up an AMI's actual root device name, for a correct ``BlockDeviceMappings`` entry.

        Without this, overriding the root volume's size/type/encryption at
        launch would either target the wrong device (creating an extra,
        unwanted volume) or be rejected outright -- AWS never lets you guess.
        """
        with aws_error_boundary("ec2", "DescribeImages"):
            response = self._client().describe_images(ImageIds=[image_id])
        images = response.get("Images", [])
        if images and images[0].get("RootDeviceName"):
            return str(images[0]["RootDeviceName"])
        return _DEFAULT_ROOT_DEVICE_NAME

    def deregister_image(self: Self, image_id: str, *, region: str | None = None) -> None:
        """Deregister an AMI. Does not delete its backing EBS snapshot(s)."""
        with aws_error_boundary("ec2", "DeregisterImage"):
            self._client(region=region).deregister_image(ImageId=image_id)

    # -- Instances ------------------------------------------------------------------

    def describe_instances(
        self: Self,
        instance_ids: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Instance]:
        """List instances, fully paginated (``Reservations[].Instances[]`` flattened).

        When ``region`` is given, every returned ``Instance.region`` is
        stamped with it -- so a region-pinned fetch always carries its own
        region forward, the same way ``describe_instances_all_regions``
        does, and a follow-up action on that instance knows which region's
        client to use without the caller re-threading it by hand.
        """
        kwargs: dict[str, Any] = {}
        if instance_ids:
            kwargs["InstanceIds"] = list(instance_ids)
        if filters:
            kwargs["Filters"] = _to_filters(filters)

        instances: list[Instance] = []
        with aws_error_boundary("ec2", "DescribeInstances"):
            paginator = self._client(region=region).get_paginator("describe_instances")
            for page in paginator.paginate(**kwargs):
                for raw in _flatten_reservations(page.get("Reservations", [])):
                    instance = Instance.model_validate(raw)
                    if region is not None:
                        instance = instance.model_copy(update={"region": region})
                    instances.append(instance)
        return instances

    def get_instance(self: Self, instance_id: str, *, region: str | None = None) -> Instance:
        """Fetch a single instance by ID."""
        instances = self.describe_instances([instance_id], None, region=region)
        # AWS itself raises InvalidInstanceID.NotFound (mapped to
        # ResourceNotFoundError) before this could ever be empty in practice.
        return instances[0]

    def _list_regions(self: Self) -> list[str]:
        """Every region name available to this account (opted-in + always-on)."""
        with aws_error_boundary("ec2", "DescribeRegions"):
            response = self._client().describe_regions(AllRegions=False)
        return [region["RegionName"] for region in response.get("Regions", [])]

    def _describe_instances_in_region(
        self: Self, region: str, filters: Mapping[str, Sequence[str]] | None
    ) -> list[Instance]:
        kwargs: dict[str, Any] = {}
        if filters:
            kwargs["Filters"] = _to_filters(filters)

        client = self.client_factory.ec2(region=region)
        instances: list[Instance] = []
        with aws_error_boundary("ec2", "DescribeInstances"):
            paginator = client.get_paginator("describe_instances")
            for page in paginator.paginate(**kwargs):
                instances.extend(
                    Instance.model_validate(raw).model_copy(update={"region": region})
                    for raw in _flatten_reservations(page.get("Reservations", []))
                )
        return instances

    def describe_instances_all_regions(
        self: Self, filters: Mapping[str, Sequence[str]] | None
    ) -> list[Instance]:
        """List instances matching ``filters`` across every region, fetched in parallel.

        A region this account can't call ``DescribeInstances`` against
        (unauthorized, opted out, or otherwise erroring) is skipped with a
        logged warning rather than failing the whole scan -- one bad region
        must never hide every other region's instances.
        """
        regions = self._list_regions()
        instances: list[Instance] = []
        with ThreadPoolExecutor(max_workers=len(regions) or 1) as executor:
            future_to_region = {
                executor.submit(self._describe_instances_in_region, region, filters): region
                for region in regions
            }
            for future in as_completed(future_to_region):
                region = future_to_region[future]
                try:
                    instances.extend(future.result())
                except AwsError as exc:
                    _logger.warning(
                        "Skipping region '%s' (DescribeInstances failed): %s", region, exc
                    )
        return instances

    # -- Key pairs --------------------------------------------------------------------

    def create_key_pair(self: Self, key_name: str, key_type: str) -> KeyMaterial:
        """Create a new key pair; AWS generates and returns the private key exactly once."""
        with aws_error_boundary("ec2", "CreateKeyPair"):
            response = self._client().create_key_pair(KeyName=key_name, KeyType=key_type)
        return KeyMaterial(key_name=key_name, private_key=response["KeyMaterial"])

    def import_key_pair(self: Self, key_name: str, public_key_material: str) -> KeyPairInfo:
        """Import an existing public key as a named key pair."""
        with aws_error_boundary("ec2", "ImportKeyPair"):
            response = self._client().import_key_pair(
                KeyName=key_name, PublicKeyMaterial=public_key_material.encode("utf-8")
            )
        return KeyPairInfo.model_validate(response)

    def describe_key_pairs(self: Self, key_names: Sequence[str] | None) -> list[KeyPairInfo]:
        """List key pairs (metadata only). Not paginated -- AWS doesn't paginate this API."""
        kwargs: dict[str, Any] = {"KeyNames": list(key_names)} if key_names else {}
        with aws_error_boundary("ec2", "DescribeKeyPairs"):
            response = self._client().describe_key_pairs(**kwargs)
        return [KeyPairInfo.model_validate(raw) for raw in response.get("KeyPairs", [])]

    def delete_key_pair(self: Self, key_name: str) -> None:
        """Delete a key pair."""
        with aws_error_boundary("ec2", "DeleteKeyPair"):
            self._client().delete_key_pair(KeyName=key_name)

    # -- Launch / lifecycle -------------------------------------------------------------

    def _build_run_instances_kwargs(
        self: Self, spec: LaunchSpec, *, client_token: str, dry_run: bool
    ) -> dict[str, Any]:
        tags = dict(spec.tags)
        tags.setdefault(_MANAGED_BY_TAG_KEY, _MANAGED_BY_TAG_VALUE)
        tag_list = [{"Key": key, "Value": value} for key, value in tags.items()]

        kwargs: dict[str, Any] = {
            "ImageId": spec.image_id,
            "InstanceType": spec.instance_type,
            "MinCount": spec.min_count,
            "MaxCount": spec.max_count,
            "ClientToken": client_token,
            "DryRun": dry_run,
            "NetworkInterfaces": [_build_network_interface(spec)],
            # IMDSv2 is mandatory, not opt-in: IMDSv1 is vulnerable to SSRF-based
            # credential theft (the Capital One breach's root cause). Every instance
            # this CLI launches requires session tokens for metadata access.
            "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
            "BlockDeviceMappings": [
                {
                    "DeviceName": self._resolve_root_device_name(spec.image_id),
                    "Ebs": {
                        "VolumeSize": spec.volume_size_gb,
                        "VolumeType": spec.volume_type,
                        "Encrypted": spec.encrypted,
                        "DeleteOnTermination": True,
                    },
                }
            ],
            # Tags applied HERE, at launch, via TagSpecifications -- not via a
            # follow-up CreateTags call. A separate CreateTags leaves a window where
            # the instance exists untagged, during which any tag-based IAM policy or
            # automation (including this CLI's own --managed-only filter) would miss
            # it entirely.
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": tag_list},
                {"ResourceType": "volume", "Tags": tag_list},
            ],
        }
        if spec.key_name:
            kwargs["KeyName"] = spec.key_name
        if spec.iam_instance_profile_arn:
            kwargs["IamInstanceProfile"] = {"Arn": spec.iam_instance_profile_arn}
        if spec.user_data:
            # Plain text: boto3/botocore base64-encodes UserData internally before
            # sending the request. Encoding it ourselves here would double-encode it,
            # and the instance would boot with a blob of base64 instead of a script.
            kwargs["UserData"] = spec.user_data
        return kwargs

    def _dry_run_placeholder(self: Self, spec: LaunchSpec) -> Instance:
        """Build the success result for a dry-run: a valid launch that wasn't actually performed."""
        tags = dict(spec.tags)
        tags.setdefault(_MANAGED_BY_TAG_KEY, _MANAGED_BY_TAG_VALUE)
        return Instance(
            instance_id=_DRY_RUN_INSTANCE_ID,
            instance_type=spec.instance_type,
            state=InstanceState.PENDING,
            image_id=spec.image_id,
            subnet_id=spec.subnet_id,
            security_group_ids=list(spec.security_group_ids),
            key_name=spec.key_name,
            iam_instance_profile_arn=spec.iam_instance_profile_arn,
            launch_time=datetime.now(UTC),
            tags=[{"Key": key, "Value": value} for key, value in tags.items()],
        )

    def run_instance(self: Self, spec: LaunchSpec, *, client_token: str, dry_run: bool) -> Instance:
        """Launch an instance, or validate that a launch would succeed (``dry_run=True``).

        AWS's dry-run contract: with ``DryRun=True``, a request that would
        have succeeded raises ``ClientError(Code="DryRunOperation")`` -- that
        IS success, and must never surface as an exception to the caller. A
        request that would have failed on permissions raises
        ``UnauthorizedOperation``, which is left to propagate into
        ``aws_error_boundary`` below (already mapped to ``AccessDeniedError``)
        exactly like a real permission failure would.
        """
        kwargs = self._build_run_instances_kwargs(spec, client_token=client_token, dry_run=dry_run)

        if dry_run:
            with aws_error_boundary("ec2", "RunInstances"):
                try:
                    self._client().run_instances(**kwargs)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") == "DryRunOperation":
                        return self._dry_run_placeholder(spec)
                    raise
            # AWS always raises for DryRun=True (either DryRunOperation, meaning
            # success, or a real permission/parameter error) -- reaching this line
            # would mean a genuine launch happened despite DryRun=True.
            return self._dry_run_placeholder(spec)  # pragma: no cover -- unreachable in practice

        with aws_error_boundary("ec2", "RunInstances"):
            response = self._client().run_instances(**kwargs)
        instances = _flatten_reservations([response])
        return Instance.model_validate(instances[0])

    def start_instances(
        self: Self, instance_ids: Sequence[str], *, region: str | None = None
    ) -> list[str]:
        """Start stopped instances. Returns the IDs AWS accepted."""
        with aws_error_boundary("ec2", "StartInstances"):
            response = self._client(region=region).start_instances(
                InstanceIds=list(instance_ids)
            )
        return [item["InstanceId"] for item in response.get("StartingInstances", [])]

    def stop_instances(
        self: Self, instance_ids: Sequence[str], *, force: bool, region: str | None = None
    ) -> list[str]:
        """Stop running instances. Returns the IDs AWS accepted."""
        with aws_error_boundary("ec2", "StopInstances"):
            response = self._client(region=region).stop_instances(
                InstanceIds=list(instance_ids), Force=force
            )
        return [item["InstanceId"] for item in response.get("StoppingInstances", [])]

    def reboot_instances(
        self: Self, instance_ids: Sequence[str], *, region: str | None = None
    ) -> None:
        """Reboot running instances."""
        with aws_error_boundary("ec2", "RebootInstances"):
            self._client(region=region).reboot_instances(InstanceIds=list(instance_ids))

    def terminate_instances(
        self: Self, instance_ids: Sequence[str], *, dry_run: bool, region: str | None = None
    ) -> list[str]:
        """Terminate instances, or validate that termination would succeed (``dry_run=True``)."""
        client = self._client(region=region)
        if dry_run:
            with aws_error_boundary("ec2", "TerminateInstances"):
                try:
                    client.terminate_instances(InstanceIds=list(instance_ids), DryRun=True)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") == "DryRunOperation":
                        return list(instance_ids)
                    raise
            return list(instance_ids)  # pragma: no cover -- unreachable in practice

        with aws_error_boundary("ec2", "TerminateInstances"):
            response = client.terminate_instances(InstanceIds=list(instance_ids))
        return [item["InstanceId"] for item in response.get("TerminatingInstances", [])]

    def create_tags(
        self: Self,
        instance_ids: Sequence[str],
        tags: Mapping[str, str],
        *,
        region: str | None = None,
    ) -> None:
        """Add tags to instances, overwriting any existing tag that shares a key."""
        if not instance_ids or not tags:
            return
        tag_list = [{"Key": key, "Value": value} for key, value in tags.items()]
        with aws_error_boundary("ec2", "CreateTags"):
            self._client(region=region).create_tags(Resources=list(instance_ids), Tags=tag_list)

    def delete_tags(
        self: Self, instance_ids: Sequence[str], keys: Sequence[str], *, region: str | None = None
    ) -> None:
        """Remove tags by key from instances (values, if any, are ignored)."""
        if not instance_ids or not keys:
            return
        tag_list = [{"Key": key} for key in keys]
        with aws_error_boundary("ec2", "DeleteTags"):
            self._client(region=region).delete_tags(Resources=list(instance_ids), Tags=tag_list)

    def set_instance_security_groups(
        self: Self, instance_id: str, group_ids: Sequence[str], *, region: str | None = None
    ) -> None:
        """Replace the security groups attached to a running instance."""
        with aws_error_boundary("ec2", "ModifyInstanceAttribute"):
            self._client(region=region).modify_instance_attribute(
                InstanceId=instance_id, Groups=list(group_ids)
            )

    def wait_for_state(
        self: Self,
        instance_ids: Sequence[str],
        target: InstanceState,
        *,
        timeout_s: int,
        poll_s: int,
        region: str | None = None,
    ) -> None:
        """Block until every instance reaches ``target``, using boto3's own waiters.

        Raises:
            OperationTimeoutError: ``timeout_s`` elapsed first. The message
                names the state each instance had actually reached, so the
                caller isn't left guessing.
        """
        waiter_name = _WAITER_NAMES[target]
        max_attempts = max(1, timeout_s // poll_s)
        waiter = self._client(region=region).get_waiter(waiter_name)
        try:
            waiter.wait(
                InstanceIds=list(instance_ids),
                WaiterConfig={"Delay": poll_s, "MaxAttempts": max_attempts},
            )
        except WaiterError as exc:
            current = self.describe_instances(list(instance_ids), None, region=region)
            states = ", ".join(f"{i.instance_id}={i.state.value}" for i in current)
            raise OperationTimeoutError(
                f"Tiempo de espera agotado tras {timeout_s}s esperando el estado "
                f"'{target.value}'. Estado actual: {states}.",
                hint="Vuelve a consultar con `ec2 instance show`; puede seguir en curso.",
                service="ec2",
                operation=f"WaitFor{waiter_name}",
            ) from exc

    def get_console_output(self: Self, instance_id: str) -> str:
        """Fetch an instance's console output, base64-decoded."""
        with aws_error_boundary("ec2", "GetConsoleOutput"):
            response = self._client().get_console_output(InstanceId=instance_id)
        output = response.get("Output") or ""
        if not output:
            return ""
        try:
            return base64.b64decode(output).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return str(output)

    def create_snapshot(self: Self, volume_id: str, description: str | None) -> str:
        """Create an EBS snapshot of ``volume_id``. Returns the new snapshot's ID."""
        kwargs: dict[str, Any] = {"VolumeId": volume_id}
        if description:
            kwargs["Description"] = description
        with aws_error_boundary("ec2", "CreateSnapshot"):
            response = self._client().create_snapshot(**kwargs)
        return cast(str, response["SnapshotId"])


if TYPE_CHECKING:
    # Static conformance check: mypy fails right here if Boto3Ec2Gateway's method
    # signatures ever drift from the Ec2Gateway Protocol.
    _ec2_gateway_conformance: Ec2Gateway = cast(Boto3Ec2Gateway, None)

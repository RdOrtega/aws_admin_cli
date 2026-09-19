"""In-memory ``Ec2Gateway`` test double.

Used instead of ``Mock()`` on purpose -- see ``tests/fakes/iam.py`` for why.
Simulates the two AWS behaviors that matter most for these tests:
``ClientToken`` deduplication (``run_instance`` with a repeated token returns
the SAME instance) and the PENDING -> (target) state transition
``wait_for_state`` performs. Counts every method call so tests can verify
caching behavior in collaborators built on top of this fake.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch

from aws_admin_cli.core.exceptions import ResourceAlreadyExistsError, ResourceNotFoundError
from aws_admin_cli.domain.models.ec2 import (
    Ami,
    Instance,
    InstanceState,
    KeyMaterial,
    KeyPairInfo,
    LaunchSpec,
)


def _tag_list(tags: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"Key": key, "Value": value} for key, value in tags.items()]


@dataclass
class FakeEc2Gateway:
    """A structurally-typed ``Ec2Gateway`` double, backed by plain dicts."""

    amis: dict[str, Ami] = field(default_factory=dict)
    instances: dict[str, Instance] = field(default_factory=dict)
    key_pairs: dict[str, KeyPairInfo] = field(default_factory=dict)
    key_materials: dict[str, str] = field(default_factory=dict)
    client_tokens: dict[str, str] = field(default_factory=dict)
    call_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _next_instance_number: int = field(default=1, init=False, repr=False)
    _next_ami_number: int = field(default=0, init=False, repr=False)

    # -- AMIs -----------------------------------------------------------------------

    def describe_images(
        self,
        image_ids: Sequence[str] | None,
        owners: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Ami]:
        del region
        self.call_counts["describe_images"] += 1
        images = list(self.amis.values())
        if image_ids:
            images = [ami for ami in images if ami.image_id in image_ids]
        if owners:
            images = [ami for ami in images if ami.owner_id in owners]
        if filters and "name" in filters:
            patterns = filters["name"]
            images = [
                ami for ami in images if any(fnmatch(ami.name, pattern) for pattern in patterns)
            ]
        return images

    def describe_images_all_regions(
        self, owners: Sequence[str] | None, filters: Mapping[str, Sequence[str]] | None
    ) -> list[Ami]:
        """Same filtering as ``describe_images`` -- this fake holds no per-region
        partitioning, so the actual multi-region fan-out is only exercised against
        the real ``Boto3Ec2Gateway``.
        """
        self.call_counts["describe_images_all_regions"] += 1
        return self.describe_images(None, owners, filters)

    def deregister_image(self, image_id: str, *, region: str | None = None) -> None:
        del region
        self.call_counts["deregister_image"] += 1
        self.amis.pop(image_id, None)

    def _next_image_id(self) -> str:
        self._next_ami_number += 1
        return f"ami-{self._next_ami_number:08x}"

    def create_image(
        self,
        instance_id: str,
        name: str,
        description: str | None,
        *,
        no_reboot: bool,
        tags: Mapping[str, str] | None = None,
        region: str | None = None,
    ) -> Ami:
        del instance_id, no_reboot, region
        self.call_counts["create_image"] += 1
        ami = Ami(
            image_id=self._next_image_id(),
            name=name,
            description=description or "",
            architecture="x86_64",
            platform_details="Linux/UNIX",
            root_device_type="ebs",
            creation_date=datetime.now(UTC),
            owner_id="000000000000",
            state="available",
            tags=_tag_list(tags or {}),
        )
        self.amis[ami.image_id] = ami
        return ami

    def copy_image(
        self,
        source_image_id: str,
        *,
        source_region: str,
        name: str,
        description: str | None,
        target_region: str,
        kms_key_id: str | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> Ami:
        del source_image_id, source_region, target_region, kms_key_id
        self.call_counts["copy_image"] += 1
        ami = Ami(
            image_id=self._next_image_id(),
            name=name,
            description=description or "",
            architecture="x86_64",
            platform_details="Linux/UNIX",
            root_device_type="ebs",
            creation_date=datetime.now(UTC),
            owner_id="000000000000",
            state="available",
            tags=_tag_list(tags or {}),
        )
        self.amis[ami.image_id] = ami
        return ami

    # -- Instances --------------------------------------------------------------------

    def describe_instances(
        self,
        instance_ids: Sequence[str] | None,
        filters: Mapping[str, Sequence[str]] | None,
        *,
        region: str | None = None,
    ) -> list[Instance]:
        del region
        self.call_counts["describe_instances"] += 1
        instances = list(self.instances.values())
        if instance_ids:
            instances = [i for i in instances if i.instance_id in instance_ids]
        if filters:
            if "instance-state-name" in filters:
                states = set(filters["instance-state-name"])
                instances = [i for i in instances if i.state.value in states]
            if "vpc-id" in filters:
                vpc_ids = set(filters["vpc-id"])
                instances = [i for i in instances if i.vpc_id in vpc_ids]
            if "subnet-id" in filters:
                subnet_ids = set(filters["subnet-id"])
                instances = [i for i in instances if i.subnet_id in subnet_ids]
            if "tag:Name" in filters:
                names = set(filters["tag:Name"])
                instances = [i for i in instances if i.name in names]
        return instances

    def describe_instances_all_regions(
        self, filters: Mapping[str, Sequence[str]] | None
    ) -> list[Instance]:
        """Same filtering as ``describe_instances`` -- this fake holds no per-region
        partitioning, so the actual multi-region fan-out is only exercised against
        the real ``Boto3Ec2Gateway``.
        """
        self.call_counts["describe_instances_all_regions"] += 1
        return self.describe_instances(None, filters)

    def get_instance(self, instance_id: str, *, region: str | None = None) -> Instance:
        del region
        self.call_counts["get_instance"] += 1
        instance = self.instances.get(instance_id)
        if instance is None:
            raise ResourceNotFoundError(
                f"La instancia '{instance_id}' no existe.", aws_code="InvalidInstanceID.NotFound"
            )
        return instance

    # -- Key pairs ----------------------------------------------------------------------

    def create_key_pair(self, key_name: str, key_type: str) -> KeyMaterial:
        self.call_counts["create_key_pair"] += 1
        if key_name in self.key_pairs:
            raise ResourceAlreadyExistsError(
                f"La key pair '{key_name}' ya existe.", aws_code="InvalidKeyPair.Duplicate"
            )
        info = KeyPairInfo(
            key_name=key_name,
            key_pair_id=f"key-{key_name}",
            key_fingerprint=f"fp:{key_name}",
            key_type=key_type,
        )
        material = f"FAKE-PRIVATE-KEY-MATERIAL-{key_name}"
        self.key_pairs[key_name] = info
        self.key_materials[key_name] = material
        return KeyMaterial(key_name=key_name, private_key=material)

    def import_key_pair(self, key_name: str, public_key_material: str) -> KeyPairInfo:
        del public_key_material
        info = KeyPairInfo(
            key_name=key_name, key_pair_id=f"key-{key_name}", key_fingerprint=f"fp:{key_name}"
        )
        self.key_pairs[key_name] = info
        return info

    def describe_key_pairs(self, key_names: Sequence[str] | None) -> list[KeyPairInfo]:
        infos = list(self.key_pairs.values())
        if key_names:
            infos = [info for info in infos if info.key_name in key_names]
        return infos

    def delete_key_pair(self, key_name: str) -> None:
        self.key_pairs.pop(key_name, None)
        self.key_materials.pop(key_name, None)

    # -- Launch / lifecycle -------------------------------------------------------------

    def run_instance(self, spec: LaunchSpec, *, client_token: str, dry_run: bool) -> Instance:
        self.call_counts["run_instance"] += 1

        if dry_run:
            return Instance(
                instance_id="dry-run",
                instance_type=spec.instance_type,
                state=InstanceState.PENDING,
                image_id=spec.image_id,
                subnet_id=spec.subnet_id,
                security_group_ids=list(spec.security_group_ids),
                key_name=spec.key_name,
                iam_instance_profile_arn=spec.iam_instance_profile_arn,
                launch_time=datetime.now(UTC),
                tags=_tag_list(spec.tags),
            )

        # AWS's real ClientToken deduplication: a repeated token within the
        # window returns the SAME instance instead of creating a second one.
        existing_id = self.client_tokens.get(client_token)
        if existing_id is not None:
            return self.instances[existing_id]

        instance_id = f"i-{self._next_instance_number:017x}"
        self._next_instance_number += 1
        instance = Instance(
            instance_id=instance_id,
            instance_type=spec.instance_type,
            state=InstanceState.PENDING,
            image_id=spec.image_id,
            subnet_id=spec.subnet_id,
            security_group_ids=list(spec.security_group_ids),
            key_name=spec.key_name,
            iam_instance_profile_arn=spec.iam_instance_profile_arn,
            launch_time=datetime.now(UTC),
            tags=_tag_list(spec.tags),
        )
        self.instances[instance_id] = instance
        self.client_tokens[client_token] = instance_id
        return instance

    def start_instances(
        self, instance_ids: Sequence[str], *, region: str | None = None
    ) -> list[str]:
        del region
        for instance_id in instance_ids:
            self._set_state(instance_id, InstanceState.PENDING)
        return list(instance_ids)

    def stop_instances(
        self, instance_ids: Sequence[str], *, force: bool, region: str | None = None
    ) -> list[str]:
        del force, region
        for instance_id in instance_ids:
            self._set_state(instance_id, InstanceState.STOPPING)
        return list(instance_ids)

    def reboot_instances(self, instance_ids: Sequence[str], *, region: str | None = None) -> None:
        del instance_ids, region

    def terminate_instances(
        self, instance_ids: Sequence[str], *, dry_run: bool, region: str | None = None
    ) -> list[str]:
        del region
        if not dry_run:
            for instance_id in instance_ids:
                self._set_state(instance_id, InstanceState.SHUTTING_DOWN)
        return list(instance_ids)

    def create_tags(
        self,
        instance_ids: Sequence[str],
        tags: Mapping[str, str],
        *,
        region: str | None = None,
    ) -> None:
        del region
        for instance_id in instance_ids:
            instance = self.instances.get(instance_id)
            if instance is None:
                continue
            merged = {tag["Key"]: tag["Value"] for tag in instance.tags}
            merged.update(tags)
            self.instances[instance_id] = instance.model_copy(update={"tags": _tag_list(merged)})

    def delete_tags(
        self, instance_ids: Sequence[str], keys: Sequence[str], *, region: str | None = None
    ) -> None:
        del region
        for instance_id in instance_ids:
            instance = self.instances.get(instance_id)
            if instance is None:
                continue
            remaining = {
                tag["Key"]: tag["Value"] for tag in instance.tags if tag["Key"] not in keys
            }
            self.instances[instance_id] = instance.model_copy(
                update={"tags": _tag_list(remaining)}
            )

    def set_instance_security_groups(
        self, instance_id: str, group_ids: Sequence[str], *, region: str | None = None
    ) -> None:
        del region
        instance = self.instances.get(instance_id)
        if instance is not None:
            self.instances[instance_id] = instance.model_copy(
                update={"security_group_ids": list(group_ids)}
            )

    def wait_for_state(
        self,
        instance_ids: Sequence[str],
        target: InstanceState,
        *,
        timeout_s: int,
        poll_s: int,
        region: str | None = None,
    ) -> None:
        del timeout_s, poll_s, region
        self.call_counts["wait_for_state"] += 1
        for instance_id in instance_ids:
            self._set_state(instance_id, target)

    def get_console_output(self, instance_id: str) -> str:
        return f"[fake console output for {instance_id}]"

    def _set_state(self, instance_id: str, state: InstanceState) -> None:
        instance = self.instances.get(instance_id)
        if instance is not None:
            self.instances[instance_id] = instance.model_copy(update={"state": state})

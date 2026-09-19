"""Application-layer DTOs for EC2 use cases.

Frozen dataclasses, same convention as ``application/dto/{iam,s3,vpc}.py``.
"""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LaunchInstanceRequest:
    """Request to launch one (or more) EC2 instances."""

    name: str
    ami_ref: str
    instance_type: str
    subnet_ref: str
    security_group_refs: tuple[str, ...]
    vpc_ref: str | None = None
    key_name: str | None = None
    iam_role: str | None = None
    iam_instance_profile_arn: str | None = None
    user_data: str | None = None
    volume_size_gb: int = 8
    volume_type: str = "gp3"
    assign_public_ip: bool = False
    confirm_public: bool = False
    confirm_large: bool = False
    count: int = 1
    tags: dict[str, str] = field(default_factory=dict)
    wait: bool = False
    timeout_s: int = 300
    dry_run: bool = False
    client_token_nonce: str | None = None


@dataclass(frozen=True, slots=True)
class ListInstancesRequest:
    """Request to list instances, with common filters."""

    state: str | None = None
    tag_name: str | None = None
    vpc_ref: str | None = None
    subnet_ref: str | None = None
    managed_only: bool = False


@dataclass(frozen=True, slots=True)
class InstanceActionRequest:
    """Request to act (start/stop/reboot/terminate) on an already-resolved instance."""

    instance_ref: str
    force: bool = False
    dry_run: bool = False
    wait: bool = False
    timeout_s: int = 300


@dataclass(frozen=True, slots=True)
class SetInstanceTagsRequest:
    """Request to add or overwrite tags on an instance."""

    instance_id: str
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeleteInstanceTagsRequest:
    """Request to remove tags (by key) from an instance."""

    instance_id: str
    keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SetInstanceSecurityGroupsRequest:
    """Request to replace the security groups attached to a running instance."""

    instance_id: str
    group_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CreateKeyPairRequest:
    """Request to create a new EC2 key pair and save its private material locally."""

    name: str
    destination_dir: Path
    key_type: str = "rsa"


@dataclass(frozen=True, slots=True)
class CreateAmiRequest:
    """Request to create an AMI from a running/stopped instance."""

    instance_id: str
    name: str
    description: str | None = None
    no_reboot: bool = True
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CopyAmiRequest:
    """Request to copy an AMI into another region."""

    source_image_id: str
    name: str
    target_region: str
    description: str | None = None
    kms_key_id: str | None = None
